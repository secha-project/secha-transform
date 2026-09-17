"""Generated programs run isolated, and anything reaching outside the task is refused."""

from __future__ import annotations

import pytest
from codegen.sandbox import execute, guard

ECHO = "import json, sys\njson.dump({'rows': [], 'stats': json.load(sys.stdin)}, sys.stdout)\n"


@pytest.mark.parametrize(
    "source",
    [
        "import json, sys, hashlib, datetime, math, re, collections\n",
        "from datetime import UTC, datetime\nfrom collections.abc import Iterable\n",
        "import re\nre.compile('x')\n",
        "import ast\nast.literal_eval('[1, 2]')\n",  # refused once, which was the harness's fault
        "import csv, contextlib, random, unicodedata, traceback\n",
    ],
)
def test_guard_allows_the_standard_library_a_transformer_needs(source):
    assert guard(source) == []


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("import os\n", "imports 'os'"),
        ("from subprocess import run\n", "imports from 'subprocess'"),
        ("import urllib.request\n", "imports 'urllib.request'"),
        ("import io\n", "imports 'io'"),  # io.open is open by another name
        ("from pathlib import Path\n", "imports from 'pathlib'"),
        ("from . import sibling\n", "imports from '.'"),
        ("open('x.txt', 'w')\n", "calls open()"),
        ("eval('1 + 1')\n", "calls eval()"),
        ("__import__('os')\n", "calls __import__()"),
        ("().__class__.__base__.__subclasses__()\n", "accesses __subclasses__"),
    ],
)
def test_guard_refuses_what_reaches_outside_the_task(source, reason):
    assert any(reason in violation for violation in guard(source))


def test_guard_reports_a_syntax_error():
    assert guard("def broken(:\n")[0].startswith("syntax error")


def test_a_refused_program_is_never_run():
    execution = execute("import os\nos.remove('anything')\n", {})
    assert execution.status == "guard_rejected" and execution.output is None


def test_a_program_reads_stdin_and_writes_json():
    execution = execute(ECHO, {"records_in": 3})
    assert execution.status == "ok"
    assert execution.output == {"rows": [], "stats": {"records_in": 3}}


def test_a_crash_is_reported_with_its_error():
    execution = execute("raise ValueError('boom')\n", {})
    assert execution.status == "crashed" and "ValueError" in execution.stderr


def test_a_traceback_is_the_same_on_every_run_and_names_no_local_path():
    source = "import json\n\ndef main():\n    json.loads(missing)\n\nmain()\n"
    first, second = execute(source, {}), execute(source, {})
    assert first.status == "crashed" and first.stderr == second.stderr
    assert 'File "program.py", line 4, in main' in first.stderr
    assert "secha-codegen-" not in first.stderr and "Temp" not in first.stderr


def test_output_that_is_not_json_is_reported():
    assert execute("print('not json')\n", {}).status == "bad_output"


def test_a_program_that_never_finishes_is_stopped():
    assert execute("while True:\n    pass\n", {}, timeout_s=1.0).status == "timeout"


def test_the_child_interpreter_has_no_site_packages_and_a_fixed_hash_seed():
    source = (
        "import json, sys\n"
        "json.dump({'site': any('site-packages' in p for p in sys.path),\n"
        "           'no_site': sys.flags.no_site, 'no_user_site': sys.flags.no_user_site,\n"
        "           'hash_randomization': sys.flags.hash_randomization}, sys.stdout)\n"
    )
    execution = execute(source, {})
    assert execution.status == "ok"
    assert execution.output == {
        "site": False,
        "no_site": 1,
        "no_user_site": 1,
        "hash_randomization": 0,
    }
