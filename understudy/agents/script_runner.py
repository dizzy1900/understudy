"""Runs a compiled fix script against one node through the jailed tool layer."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from config import settings
from understudy.agents import script_child
from understudy.agents.protocol import TOOL_NAMES
from understudy.agents.tools import ToolBox

SCRIPT_TIMEOUT_S = 30.0
MAX_CALLS = 40
RESPONSE_CHARS = script_child.RESPONSE_CHARS
SCRIPT_TOOLS = tuple(name for name in TOOL_NAMES if name != "claim_fixed")
CHILD = Path(script_child.__file__).resolve()


@dataclass
class ScriptResult:
    ok: bool
    summary: str = ""
    root_cause: str = ""
    error: str = ""
    calls: int = 0


async def run_script(tools: ToolBox, path: Path, timeout: float = SCRIPT_TIMEOUT_S) -> ScriptResult:
    """The child gets an empty environment apart from PATH, so it cannot read the API key."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(CHILD), str(path), cwd=settings.ROOT, env={"PATH": os.environ.get("PATH", "")},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    calls = 0
    try:
        async with asyncio.timeout(timeout):
            assert proc.stdout is not None and proc.stdin is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return ScriptResult(False, error=f"the script exited without a result (code {await proc.wait()})", calls=calls)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("done"):
                    return ScriptResult(True, str(message.get("summary", "")), str(message.get("root_cause", "")), calls=calls)
                if "error" in message:
                    return ScriptResult(False, error=str(message["error"]), calls=calls)
                tool = message.get("tool")
                args = message.get("args") if isinstance(message.get("args"), dict) else {}
                if tool not in SCRIPT_TOOLS or calls >= MAX_CALLS:
                    reply = {"ok": False, "text": f"tool {tool!r} is not available to scripts" if tool not in SCRIPT_TOOLS else "too many tool calls"}
                else:
                    calls += 1
                    result = await tools.call(calls, tool, args)
                    reply = {"ok": result.ok, "text": result.text[:RESPONSE_CHARS]}
                proc.stdin.write((json.dumps(reply) + "\n").encode())
                await proc.stdin.drain()
    except TimeoutError:
        return ScriptResult(False, error=f"the script did not finish within {timeout:.0f}s", calls=calls)
    except (BrokenPipeError, ConnectionResetError):
        return ScriptResult(False, error="the script closed the tool channel", calls=calls)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
