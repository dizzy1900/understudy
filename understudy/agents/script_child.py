"""Runs a compiled fix script in its own process.

The script defines fix(tools) and may import only re, json and yaml. Every tools.<name>(...) call is sent to the
parent over stdout as one JSON line and answered on stdin, so the script reaches the node only through the same
jailed tool layer the model-driven responders use. This module imports nothing from the package so it can run
under `python -I`.
"""
from __future__ import annotations

import ast
import builtins
import json
import re
import sys
from typing import Any

import yaml

ALLOWED_IMPORTS = {"re", "json", "yaml"}
BANNED_NAMES = {
    "exec", "eval", "compile", "open", "getattr", "setattr", "delattr", "globals", "locals", "vars", "input",
    "breakpoint", "memoryview", "type", "object", "super", "classmethod", "staticmethod", "property", "dir",
}
BANNED_ATTRS = {"load", "load_all", "unsafe_load", "unsafe_load_all", "full_load", "full_load_all", "Loader", "UnsafeLoader", "FullLoader"}
SAFE_BUILTINS = (
    "abs all any bool bytes dict enumerate filter float int isinstance len list map max min range repr reversed round "
    "set sorted str sum tuple zip chr ord hex divmod pow slice"
).split()
SAFE_EXCEPTIONS = "Exception ValueError KeyError IndexError TypeError RuntimeError AssertionError StopIteration".split()
RESPONSE_CHARS = 8000


class ScriptRejected(Exception):
    pass


def check_source(source: str) -> None:
    """Static screen. It is a guard against mistakes in a generated script, not a hardened sandbox: the
    subprocess, its empty environment and the 30 second limit carry the rest."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ScriptRejected(f"syntax error on line {exc.lineno}: {exc.msg}") from None
    has_fix = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    raise ScriptRejected(f"import of {alias.name!r} is not allowed; only re, json and yaml")
        elif isinstance(node, ast.ImportFrom):
            if node.module not in ALLOWED_IMPORTS or node.level:
                raise ScriptRejected(f"import from {node.module!r} is not allowed; only re, json and yaml")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ScriptRejected(f"access to private attribute {node.attr!r} is not allowed")
            if node.attr in BANNED_ATTRS:
                raise ScriptRejected(f"{node.attr!r} is not allowed; use yaml.safe_load")
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES or (node.id.startswith("__") and node.id != "__name__"):
                raise ScriptRejected(f"use of {node.id!r} is not allowed")
        elif isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.Global, ast.Nonlocal)):
            raise ScriptRejected(f"{type(node).__name__} is not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "fix":
            has_fix = True
    if not has_fix:
        raise ScriptRejected("the script must define fix(tools)")


def _safe_import(name: str, globals_: Any = None, locals_: Any = None, fromlist: Any = (), level: int = 0) -> Any:
    if name not in ALLOWED_IMPORTS or level:
        raise ImportError(f"import of {name!r} is not allowed")
    return {"re": re, "json": json, "yaml": yaml}[name]


class Result:
    def __init__(self, ok: bool, text: str):
        self.ok = ok
        self.text = text

    def __str__(self) -> str:
        return self.text


class Tools:
    def __init__(self, send: Any, receive: Any):
        self._send = send
        self._receive = receive

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def call(**kwargs: Any) -> Result:
            self._send({"tool": name, "args": kwargs})
            reply = self._receive()
            return Result(bool(reply.get("ok")), str(reply.get("text", "")))

        return call


def run(path: str) -> int:
    proto_out = sys.stdout
    sys.stdout = sys.stderr

    def send(message: dict[str, Any]) -> None:
        proto_out.write(json.dumps(message) + "\n")
        proto_out.flush()

    def receive() -> dict[str, Any]:
        line = sys.stdin.readline()
        if not line:
            raise RuntimeError("the parent closed the tool channel")
        return json.loads(line)

    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        check_source(source)
        env: dict[str, Any] = {name: getattr(builtins, name) for name in SAFE_BUILTINS + SAFE_EXCEPTIONS}
        env.update({"__import__": _safe_import, "print": lambda *a, **k: print(*a, file=sys.stderr)})
        namespace: dict[str, Any] = {"__builtins__": env, "__name__": "fix_script"}
        exec(compile(source, path, "exec"), namespace)
        summary, root_cause = namespace["fix"](Tools(send, receive))
        if not isinstance(summary, str) or not isinstance(root_cause, str) or not root_cause.strip():
            raise ScriptRejected("fix(tools) must return (summary, root_cause) as two non-empty strings")
    except BaseException as exc:
        send({"error": f"{type(exc).__name__}: {exc}"[:400]})
        return 1
    send({"done": True, "summary": summary[:300], "root_cause": root_cause[:300]})
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
