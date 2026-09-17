"""The client: code extraction, endpoint choice, streaming, and telling provider from model."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error

import pytest
from codegen import client as client_module
from codegen.client import (
    AVIARY,
    MISTRAL,
    NVIDIA,
    ChatClient,
    endpoint_for,
    extract_code,
    prompt_truncated,
    read_event_stream,
    streams,
)

MESSAGES = [{"role": "user", "content": "x" * 24000}]


def test_extracts_the_longest_python_block():
    reply = (
        "Here:\n```python\nprint(1)\n```\nand the real one\n```python\nimport json\nprint(2)\n```"
    )
    assert extract_code(reply) == "import json\nprint(2)\n"


def test_falls_back_to_an_untagged_block():
    assert extract_code("```\nprint(3)\n```") == "print(3)\n"


def test_ignores_a_reasoning_trace():
    reply = "<think>```python\nwrong()\n```</think>\n```python\nright()\n```"
    assert extract_code(reply) == "right()\n"


def test_keeps_a_program_cut_off_by_the_token_limit():
    assert extract_code("```python\ndef transform(payload):\n    rows = [") == (
        "def transform(payload):\n    rows = [\n"
    )


def test_accepts_bare_code_and_rejects_prose():
    assert extract_code("import json\nprint(json.dumps({}))") is not None
    assert extract_code("I cannot write that program.") is None
    assert extract_code(None) is None


@pytest.mark.parametrize(
    ("model", "endpoint"),
    [
        ("phi4-14b", AVIARY),
        ("mistral-medium-latest", MISTRAL),
        ("mistral-medium-2604", MISTRAL),
        ("moonshotai/kimi-k3", NVIDIA),
    ],
)
def test_endpoint_follows_the_model_name(model, endpoint):
    assert endpoint_for(model) == endpoint


@pytest.mark.parametrize(
    ("usage", "chars", "truncated"),
    [
        ({"prompt_tokens": 4096}, 23829, True),  # cut to the replica's window
        ({"prompt_tokens": 5000}, 23829, True),  # far too few tokens for these characters
        ({"prompt_tokens": 7800}, 23829, False),  # the whole specification prompt
        ({"prompt_tokens": 8192}, 40000, True),
        ({}, 23829, False),  # nothing reported, nothing to check
        ({"prompt_tokens": 0}, 23829, False),
    ],
)
def test_prompt_truncation_is_detected_from_the_providers_count(usage, chars, truncated):
    assert prompt_truncated(usage, chars) is truncated


def _reply(prompt_tokens: int) -> dict:
    return {
        "choices": [{"message": {"content": "```python\nprint(1)\n```"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens},
    }


def test_a_truncated_reply_is_resent_and_never_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("SECHA_AVIARY_API_KEY", "test-key")
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)
    replies = iter([_reply(4096), _reply(4096), _reply(7800)])
    chat = ChatClient(tmp_path)
    monkeypatch.setattr(chat, "_post", lambda *_args: next(replies))

    completion = chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert completion.text is not None and completion.truncation_retries == 2
    assert len(list(tmp_path.rglob("*.json"))) == 1

    def fail(*_args: object) -> dict:
        raise AssertionError("a cached reply must not reach the network")

    monkeypatch.setattr(chat, "_post", fail)
    again = chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert again.cached and again.text == completion.text and again.truncation_retries == 2


def test_a_prompt_that_is_always_truncated_ends_in_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SECHA_AVIARY_API_KEY", "test-key")
    chat = ChatClient(tmp_path, truncation_retries=2)
    monkeypatch.setattr(chat, "_post", lambda *_args: _reply(4096))
    completion = chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert completion.text is None and "truncated" in completion.error
    assert completion.truncation_retries == 3 and not list(tmp_path.rglob("*.json"))
    assert completion.provider_failure


def test_a_missing_key_fails_without_touching_the_network(tmp_path, monkeypatch):
    monkeypatch.delenv("SECHA_AVIARY_API_KEY", raising=False)
    monkeypatch.delenv("SECHA_LLM_API_KEY", raising=False)
    completion = ChatClient(tmp_path).complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert completion.text is None and "no API key" in completion.error
    assert completion.provider_failure


def _http_error(status: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError(AVIARY, status, "error", headers, io.BytesIO(b"{}"))


def _sequence(*items: object):
    queue = iter(items)

    def post(*_args: object) -> object:
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        return item

    return post


def test_a_busy_provider_is_waited_out_on_a_budget_of_its_own(tmp_path, monkeypatch):
    monkeypatch.setenv("SECHA_AVIARY_API_KEY", "test-key")
    pauses: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", pauses.append)
    chat = ChatClient(tmp_path, retries=3)
    busy = [_http_error(429, "7"), _http_error(504), _http_error(429), _http_error(503)]
    monkeypatch.setattr(chat, "_post", _sequence(*busy, _reply(7800)))

    completion = chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert completion.text is not None and not completion.provider_failure
    assert pauses == [7.0, 30.0, 60.0, 120.0]  # the provider's own pause, then doubling


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_a_provider_busy_past_the_budget_is_a_provider_failure(tmp_path, monkeypatch, status):
    monkeypatch.setenv("SECHA_AVIARY_API_KEY", "test-key")
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)
    chat = ChatClient(tmp_path, busy_wait_s=100)
    monkeypatch.setattr(chat, "_post", _sequence(*[_http_error(status) for _ in range(10)]))

    completion = chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert completion.text is None and completion.provider_failure
    assert f"HTTP {status}" in completion.error and not list(tmp_path.rglob("*.json"))


def test_a_server_that_keeps_failing_is_a_provider_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("SECHA_AVIARY_API_KEY", "test-key")
    monkeypatch.setattr(client_module.time, "sleep", lambda _seconds: None)
    chat = ChatClient(tmp_path, retries=3)
    broken = ConnectionError("the stream ended before the reply was complete")
    monkeypatch.setattr(chat, "_post", _sequence(_http_error(500), broken, _http_error(500)))

    completion = chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert completion.text is None and completion.provider_failure
    assert "HTTP 500" in completion.error


@pytest.mark.parametrize(
    ("reason", "provider_failure"), [("length", False), ("stop", True), ("content_filter", True)]
)
def test_an_empty_reply_is_the_models_failure_only_when_its_budget_ran_out(
    tmp_path, monkeypatch, reason, provider_failure
):
    monkeypatch.setenv("SECHA_AVIARY_API_KEY", "test-key")
    chat = ChatClient(tmp_path)
    empty = {
        "choices": [{"message": {"content": ""}, "finish_reason": reason}],
        "usage": {"prompt_tokens": 7800},
    }
    monkeypatch.setattr(chat, "_post", lambda *_args: empty)
    completion = chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert completion.text is None and completion.provider_failure is provider_failure


def _stream(*chunks: object) -> list[bytes]:
    lines = [b": keep-alive\n", b"\n"]
    for chunk in chunks:
        data = chunk if isinstance(chunk, str) else json.dumps(chunk)
        lines += [f"data: {data}\n".encode(), b"\n"]
    return lines


def _delta(content: str | None = None, reasoning: str | None = None, finish: str | None = None):
    delta = {"content": content, "reasoning_content": reasoning}
    return {"choices": [{"delta": {k: v for k, v in delta.items() if v}, "finish_reason": finish}]}


def test_a_stream_is_assembled_into_one_reply_without_its_reasoning():
    lines = _stream(
        _delta(reasoning="The rulebook maps fhz to frequency."),
        _delta(content="```python\nprint("),
        _delta(content="1)\n```"),
        _delta(finish="stop"),
        {"choices": [], "model": "kimi-k3", "usage": {"prompt_tokens": 7273}},
        "[DONE]",
    )
    payload = read_event_stream(lines)
    choice = payload["choices"][0]
    assert choice["message"]["content"] == "```python\nprint(1)\n```"
    assert choice["finish_reason"] == "stop" and payload["usage"]["prompt_tokens"] == 7273
    assert payload["model"] == "kimi-k3"


@pytest.mark.parametrize(
    "chunks",
    [
        (_delta(content="```python\nprint("),),  # the connection closed mid-reply
        (_delta(content="x"), {"error": {"message": "overloaded"}}),
        ("{not json",),
    ],
)
def test_a_stream_that_does_not_finish_cleanly_is_a_connection_failure(chunks):
    with pytest.raises(ConnectionError):
        read_event_stream(_stream(*chunks))


def test_only_nvidia_is_streamed_and_the_cache_key_ignores_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SECHA_NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("SECHA_AVIARY_API_KEY", "test-key")
    sent: list[dict] = []

    def post(_endpoint: str, _key: str, body: bytes) -> dict:
        sent.append(json.loads(body))
        return _reply(7800)

    chat = ChatClient(tmp_path)
    monkeypatch.setattr(chat, "_post", post)
    chat.complete(NVIDIA, "moonshotai/kimi-k3", MESSAGES, 0.2, 100, sample=0)
    chat.complete(AVIARY, "phi4-14b", MESSAGES, 0.2, 100, sample=0)
    assert sent[0]["stream"] is True and "stream" not in sent[1]
    assert streams(NVIDIA) and not streams(AVIARY) and not streams(MISTRAL)

    # replies cached before requests were streamed must still be found
    request = {"model": "moonshotai/kimi-k3", "messages": MESSAGES, "temperature": 0.2}
    request["max_tokens"] = 100
    identity = {"provider": "integrate-api-nvidia-com", "request": request, "sample": 0}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
    folder = tmp_path / "integrate-api-nvidia-com" / "moonshotai-kimi-k3"
    assert (folder / f"{digest[:24]}.json").exists()


def test_the_served_model_is_recorded_and_survives_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SECHA_MISTRAL_API_KEY", "test-key")
    chat = ChatClient(tmp_path)
    monkeypatch.setattr(chat, "_post", lambda *_args: _reply(7800) | {"model": "codestral-2508"})

    first = chat.complete(MISTRAL, "codestral-latest", MESSAGES, 0.2, 100, sample=0)
    again = chat.complete(MISTRAL, "codestral-latest", MESSAGES, 0.2, 100, sample=0)
    assert not first.cached and first.served_model == "codestral-2508"
    assert again.cached and again.served_model == "codestral-2508"
