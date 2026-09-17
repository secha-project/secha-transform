"""A minimal OpenAI-compatible chat client using only the standard library.

The engine's dependency footprint is part of its contract, so this experiment adds no HTTP
library to it. Replies are cached by request and sample index: at a non-zero temperature a
sample is a draw, and re-scoring must reuse that draw rather than take a new one.

A reply is accepted only when the provider demonstrably read the whole prompt. Some services
balance requests across replicas configured with different context windows, so the same
request is read in full by one replica and silently cut to 4,096 tokens by the next. A program
written from a truncated specification would be scored as the model's failure, so a truncated
reply is never cached and the request is sent again.

For the same reason a failure of the provider is kept apart from a failure of the model. A
busy or throttling provider is waited out on a budget of its own. When that budget runs out, a
server keeps failing, or the prompt is always truncated, the completion is marked as a provider
failure, and the harness records nothing for that sample rather than scoring it.

Requests to NVIDIA are streamed. Its gateway answers HTTP 504 when no response has begun after
about 300 seconds, and a reply that is not streamed begins only once it is complete, so time in
the queue and time generating would share that limit. A stream begins when generation does and
was seen to run for 659 seconds uncut. Streaming is a transport detail, so it is left out of the
cache key.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

AVIARY = "https://aviary.fgl.rd.tuni.fi/api/chat/completions"
MISTRAL = "https://api.mistral.ai/v1/chat/completions"
NVIDIA = "https://integrate.api.nvidia.com/v1/chat/completions"

KEY_ENV_BY_HOST = {
    "aviary.fgl.rd.tuni.fi": "SECHA_AVIARY_API_KEY",
    "api.mistral.ai": "SECHA_MISTRAL_API_KEY",
    "integrate.api.nvidia.com": "SECHA_NVIDIA_API_KEY",
}
STREAMING_HOSTS = frozenset({"integrate.api.nvidia.com"})
_MISTRAL_FAMILIES = {"mistral", "ministral", "magistral", "codestral", "devstral", "pixtral"}
_WINDOW_SIZES = {2048, 4096, 8192}
_BUSY_STATUSES = frozenset({429, 502, 503, 504})  # busy or throttled, not refused


def endpoint_for(model: str) -> str:
    """The default endpoint for a model name: NIM ids have a slash, Mistral ids a family."""
    if "/" in model:
        return NVIDIA
    if model.split("-")[0] in _MISTRAL_FAMILIES:
        return MISTRAL
    return AVIARY


def streams(endpoint: str) -> bool:
    return (urlparse(endpoint).hostname or "") in STREAMING_HOSTS


def provider_tag(endpoint: str) -> str:
    host = urlparse(endpoint).hostname or "unknown"
    return re.sub(r"[^A-Za-z0-9]+", "-", host).strip("-").lower()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "-", text).strip("-")


def load_env_file(path: Path) -> None:
    """Read KEY=value lines into the environment without overriding what is already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def api_key_for(endpoint: str) -> str:
    name = KEY_ENV_BY_HOST.get(urlparse(endpoint).hostname or "")
    return os.environ.get(name or "SECHA_LLM_API_KEY", "")


def prompt_truncated(usage: dict[str, Any], prompt_chars: int) -> bool:
    """Whether the provider counted fewer prompt tokens than the prompt must contain.

    Specification prompts, dense with YAML and JSON, run at about three characters per token,
    so a count below one token per 4.5 characters cannot be the whole prompt. A count sitting
    exactly on a common window size, for a prompt clearly longer than it, is the same failure.
    Plain prose runs closer to five characters per token, so a caller sending prose should turn
    this check off. A provider that reports no count cannot be checked.
    """
    counted = usage.get("prompt_tokens")
    if not isinstance(counted, int) or counted <= 0:
        return False
    if counted < prompt_chars / 4.5:
        return True
    return counted in _WINDOW_SIZES and prompt_chars / 4 > counted


@dataclass
class Completion:
    """One model reply, or the reason there is none."""

    text: str | None
    error: str = ""
    elapsed_s: float = 0.0
    cached: bool = False
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    truncation_retries: int = 0
    # The model name the provider reports serving, which can differ from the name requested when
    # that name is an alias. Empty when the provider reports none.
    served_model: str = ""
    # True when the provider, not the model, failed. Such a completion says nothing about the
    # model, so it must never be scored.
    provider_failure: bool = False


def _content_text(content: Any) -> str | None:
    if isinstance(content, list):  # some providers return content parts
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else None


def _retry_after(headers: Any) -> float | None:
    """The pause a busy provider asks for, when it gives one in seconds."""
    value = headers.get("Retry-After") if headers is not None else None
    try:
        return max(float(value), 1.0) if value is not None else None
    except ValueError:  # an HTTP date, which no provider used here sends
        return None


def _event_data(lines: Iterable[bytes]) -> Iterator[str]:
    """The data of each server-sent event, with comments and other fields read past."""
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith("data:"):
            value = line[len("data:") :]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


def read_event_stream(lines: Iterable[bytes]) -> dict[str, Any]:
    """Assemble a streamed chat completion into the shape of a reply that was not streamed.

    Only the answer is kept; reasoning deltas are read past, as they would sit in a separate
    field of a whole reply. A stream that ends without a finish reason was cut off, and a
    stream that reports an error failed, so both are raised as a connection failure and retried.
    """
    parts: list[str] = []
    finish_reason = ""
    usage: dict[str, Any] = {}
    model = ""
    for data in _event_data(lines):
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ConnectionError(f"the stream sent an unreadable event: {data[:80]!r}") from exc
        if chunk.get("error"):
            raise ConnectionError(f"the stream reported an error: {str(chunk['error'])[:200]}")
        usage = chunk.get("usage") or usage
        model = chunk.get("model") or model
        for choice in chunk.get("choices") or []:
            text = _content_text((choice.get("delta") or {}).get("content"))
            if text:
                parts.append(text)
            finish_reason = choice.get("finish_reason") or finish_reason
    if not finish_reason:
        raise ConnectionError("the stream ended before the reply was complete")
    return {
        "choices": [{"message": {"content": "".join(parts)}, "finish_reason": finish_reason}],
        "usage": usage,
        "model": model,
    }


class ChatClient:
    """Chat completions with a reply cache, retries, and a check that the prompt was read."""

    def __init__(
        self,
        cache_dir: Path,
        timeout_s: float = 900.0,
        delay_s: float = 0.0,
        retries: int = 3,
        truncation_retries: int = 8,
        check_truncation: bool = True,
        use_cache: bool = True,
        busy_wait_s: float = 1200.0,
    ) -> None:
        self._cache_dir = cache_dir
        self._timeout_s = timeout_s
        self._delay_s = delay_s
        self._retries = retries
        self._truncation_retries = truncation_retries
        self._check_truncation = check_truncation
        self._use_cache = use_cache
        self._busy_wait_s = busy_wait_s

    def _post(self, endpoint: str, key: str, body: bytes) -> dict[str, Any]:
        stream = streams(endpoint)
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "secha-llm-codegen/0.1",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        request = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
            if stream:
                return read_event_stream(response)
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return payload

    def complete(
        self,
        endpoint: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        sample: int,
    ) -> Completion:
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        identity = {"provider": provider_tag(endpoint), "request": request, "sample": sample}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        cached = self._cache_dir / provider_tag(endpoint) / slug(model) / f"{digest[:24]}.json"
        if self._use_cache and cached.exists():
            blob = json.loads(cached.read_text(encoding="utf-8"))
            return Completion(
                text=blob["text"],
                elapsed_s=float(blob.get("elapsed_s") or 0.0),
                cached=True,
                finish_reason=blob.get("finish_reason", ""),
                usage=blob.get("usage") or {},
                truncation_retries=int(blob.get("truncation_retries") or 0),
                served_model=str(blob.get("served_model") or ""),
            )

        key = api_key_for(endpoint)
        if not key:
            return Completion(
                text=None, error=f"no API key for {provider_tag(endpoint)}", provider_failure=True
            )
        wire = request
        if streams(endpoint):
            wire = request | {"stream": True, "stream_options": {"include_usage": True}}
        body = json.dumps(wire).encode("utf-8")
        prompt_chars = sum(len(message.get("content", "")) for message in messages)
        failures = 0
        truncations = 0
        busy = 0
        waited_s = 0.0
        model_outcome = False
        last_error = "unknown error"
        while failures < self._retries and truncations <= self._truncation_retries:
            if self._delay_s:
                time.sleep(self._delay_s)
            started = time.monotonic()
            try:
                payload = self._post(endpoint, key, body)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
                last_error = f"HTTP {exc.code}: {detail}"
                if exc.code in _BUSY_STATUSES:
                    pause = _retry_after(exc.headers) or min(15.0 * 2**busy, 300.0)
                    if waited_s + pause > self._busy_wait_s:
                        last_error += f" (still unavailable after waiting {waited_s:.0f} s)"
                        break
                    busy += 1
                    waited_s += pause
                    time.sleep(pause)
                    continue
                failures += 1
                if exc.code >= 500:
                    time.sleep(5 * failures)
                    continue
                break
            except OSError as exc:  # includes a stream that was cut off or reported an error
                failures += 1
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(5 * failures)
                continue
            elapsed = time.monotonic() - started

            choice = (payload.get("choices") or [{}])[0]
            text = _content_text((choice.get("message") or {}).get("content"))
            reason = choice.get("finish_reason") or ""
            usage = payload.get("usage") or {}
            served = str(payload.get("model") or "")
            if self._check_truncation and prompt_truncated(usage, prompt_chars):
                truncations += 1
                last_error = (
                    f"prompt truncated by the provider: {usage.get('prompt_tokens')} tokens "
                    f"counted for {prompt_chars} characters"
                )
                continue
            if not (text or "").strip():
                last_error = f"empty content, finish_reason={reason or 'unknown'}"
                # A model that spends its whole token budget before answering has failed at the
                # task as posed. An empty reply for any other reason is the provider's failure.
                model_outcome = reason == "length"
                break
            if self._use_cache:
                cached.parent.mkdir(parents=True, exist_ok=True)
                blob = {
                    "text": text,
                    "elapsed_s": elapsed,
                    "finish_reason": reason,
                    "usage": usage,
                    "truncation_retries": truncations,
                    "served_model": served,
                }
                cached.write_text(json.dumps(blob), encoding="utf-8")
            return Completion(
                text=text,
                elapsed_s=elapsed,
                finish_reason=reason,
                usage=usage,
                truncation_retries=truncations,
                served_model=served,
            )
        return Completion(
            text=None,
            error=last_error,
            truncation_retries=truncations,
            provider_failure=not model_outcome,
        )


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_BLOCK = re.compile(r"```([A-Za-z0-9_+-]*)[^\n]*\n(.*?)```", re.DOTALL)
_OPEN_BLOCK = re.compile(r"```(?:python|py|python3)?[^\n]*\n(.*)\Z", re.DOTALL | re.IGNORECASE)


def extract_code(text: str | None) -> str | None:
    """The program in a reply: the longest Python block, then any block, then bare code.

    A reply cut off by the token limit has an opening fence and no closing one. Its partial
    program is returned rather than discarded, so the failure is scored as a syntax error
    that can be traced to truncation instead of vanishing as "no code".
    """
    if not text:
        return None
    cleaned = _THINK.sub("", text)
    blocks = _BLOCK.findall(cleaned)
    python = [body for language, body in blocks if language.lower() in {"python", "py", "python3"}]
    chosen = python or [body for _, body in blocks]
    if chosen:
        return max(chosen, key=len).strip() + "\n"
    unterminated = _OPEN_BLOCK.search(cleaned)
    if unterminated:
        return unterminated.group(1).strip() + "\n"
    try:
        ast.parse(cleaned)
    except SyntaxError:
        return None
    return cleaned.strip() + "\n"
