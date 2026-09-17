"""Run a generated program in isolation, and refuse to run one that reaches outside its task.

This guards against accidental damage, not against a hostile program. The realistic risk is a
model that, trying to help, writes a file, reads an environment variable that happens to hold
an API key, or opens a connection. Three layers keep that from happening while a program is
scored:

1. A static check of the syntax tree refuses imports outside a standard-library allow-list,
   and calls such as `open`, `eval` and `exec`.
2. The program runs in a separate interpreter started with `-P -s -S`: no script directory on
   the path, no user site, no site-packages. Only the standard library can be imported.
3. That interpreter gets an empty working directory, an environment stripped to what Windows
   needs to start Python (no API keys, no SECHA_ paths), and a timeout.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The contract permits the whole standard library and forbids only files, network, processes
# and the environment. So a module is refused only when it offers one of those, or reaches into
# the interpreter (io.open, codecs.open, pathlib, logging handlers, os, inspect, importlib).
# Refusing a harmless module the contract allows would score the harness's rule as the model's
# failure: `ast` was missing until a phi4-14b program used ast.literal_eval.
ALLOWED_MODULES = frozenset(
    {
        "__future__",
        "abc",
        "array",
        "ast",
        "base64",
        "binascii",
        "bisect",
        "calendar",
        "collections",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "enum",
        "fractions",
        "functools",
        "graphlib",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "itertools",
        "json",
        "keyword",
        "math",
        "numbers",
        "operator",
        "pprint",
        "random",
        "re",
        "secrets",
        "shlex",
        "statistics",
        "string",
        "struct",
        "sys",
        "textwrap",
        "time",
        "traceback",
        "types",
        "typing",
        "unicodedata",
        "warnings",
        "weakref",
        "zlib",
        "zoneinfo",
    }
)
FORBIDDEN_CALLS = frozenset(
    {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint"}
)
FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "__subclasses__",
        "__globals__",
        "__builtins__",
        "__code__",
        "__closure__",
        "__loader__",
        "__spec__",
    }
)
FORBIDDEN_NAMES = frozenset({"__builtins__", "__loader__", "__spec__"})
MAX_STDERR = 4000


def guard(source: str) -> list[str]:
    """Reasons a program may not run, or an empty list when it may."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    violations.add(f"imports '{alias.name}' (line {node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or module.split(".")[0] not in ALLOWED_MODULES:
                violations.add(f"imports from '{'.' * node.level}{module}' (line {node.lineno})")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                violations.add(f"calls {node.func.id}() (line {node.lineno})")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            violations.add(f"accesses {node.attr} (line {node.lineno})")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            violations.add(f"uses {node.id} (line {node.lineno})")
    return sorted(violations)


@dataclass
class Execution:
    """What happened when a program was given one input."""

    status: str  # ok | syntax_error | guard_rejected | timeout | crashed | bad_output
    output: Any = None
    stderr: str = ""
    elapsed_s: float = 0.0
    violations: list[str] = field(default_factory=list)
    detail: str = ""


def _environment() -> dict[str, str]:
    """The least environment Python needs on this machine, with a fixed hash seed."""
    env = {"PYTHONHASHSEED": "0"}
    for name in ("SYSTEMROOT", "WINDIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def execute(source: str, payload: dict[str, Any], timeout_s: float = 60.0) -> Execution:
    """Run a program on one input and read its JSON output."""
    violations = guard(source)
    if violations:
        status = "syntax_error" if violations[0].startswith("syntax error") else "guard_rejected"
        return Execution(status=status, violations=violations)

    with tempfile.TemporaryDirectory(prefix="secha-codegen-") as workdir:
        program = Path(workdir) / "program.py"
        program.write_text(source, encoding="utf-8")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, "-P", "-s", "-S", "-X", "utf8", str(program)],
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True,
                cwd=workdir,
                env=_environment(),
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Execution(
                status="timeout",
                elapsed_s=time.monotonic() - started,
                detail=f"exceeded {timeout_s:.0f} s",
            )
        elapsed = time.monotonic() - started

    # A traceback names the program by its path in a randomly named temporary directory. Repair
    # feedback quotes the traceback, so without this the same failure would produce a different
    # repair prompt on every run, defeating the reply cache and sending a local path to a model.
    stderr = completed.stderr.decode("utf-8", errors="replace")
    stderr = stderr.replace(str(program), "program.py").replace(workdir, ".")[-MAX_STDERR:]
    if completed.returncode != 0:
        return Execution(
            status="crashed",
            stderr=stderr,
            elapsed_s=elapsed,
            detail=f"exit code {completed.returncode}",
        )
    try:
        output = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Execution(
            status="bad_output",
            stderr=stderr,
            elapsed_s=elapsed,
            detail=f"stdout is not JSON: {exc}",
        )
    return Execution(status="ok", output=output, stderr=stderr, elapsed_s=elapsed)
