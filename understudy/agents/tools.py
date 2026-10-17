"""Responder tools, confined to a single node.

File tools resolve every path against the node directory and refuse anything that lands outside it. Process
tools go through the supervisor and only touch this node. Nothing here can reach the referee or the ground
truth table.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from pydantic import ValidationError

from config import settings
from understudy.agents.protocol import TOOL_NAMES, clip
from understudy.context import Context
from understudy.events import ToolCall
from understudy.fleet.nodeconfig import ConfigError, NodeConfig, dump_config, load_config_dict, parse_config_text
from understudy.fleet.supervisor import NodeStatus

MODELS_TTL_S = 120.0
MAX_DELETE = 500
MAX_FILE_CHARS = 4000
_models_cache: dict[str, tuple[float, list[str]]] = {}


class ToolError(Exception):
    """A tool refused or failed; the message goes back to the responder."""


@dataclass
class ToolResult:
    ok: bool
    text: str
    claim: dict[str, str] | None = None


def summarize_args(args: dict[str, Any]) -> str:
    parts = []
    for key, value in args.items():
        if isinstance(value, str) and ("\n" in value or len(value) > 40):
            shown = f"<{len(value)} chars>" if key == "yaml_text" else repr(" ".join(value.split())[:40])
        else:
            shown = repr(value)
        parts.append(f"{key}={shown}")
    return ", ".join(parts)[:120]


def first_line(text: str, limit: int = 120) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line[:limit]


class ToolBox:
    def __init__(self, ctx: Context, incident_id: int, node_id: str, agent_id: str):
        self.ctx = ctx
        self.incident_id = incident_id
        self.node_id = node_id
        self.agent_id = agent_id
        self.paths = ctx.sup.paths(node_id)
        self.root = self.paths.root.resolve()
        self._handlers: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
            "node_status": self.node_status,
            "tail_log": self.tail_log,
            "read_config": self.read_config,
            "write_config": self.write_config,
            "list_dir": self.list_dir,
            "delete_files": self.delete_files,
            "fix_permissions": self.fix_permissions,
            "restart_node": self.restart_node,
            "stop_node": self.stop_node,
            "start_node": self.start_node,
            "port_owner": self.port_owner,
            "kill_squatter": self.kill_squatter,
            "set_port": self.set_port,
            "list_models": self.list_models,
            "probe_once": self.probe_once,
        }
        assert set(self._handlers) | {"claim_fixed"} == set(TOOL_NAMES)

    # dispatch -----------------------------------------------------------------------

    async def call(self, step: int, action: str, args: dict[str, Any]) -> ToolResult:
        claim: dict[str, str] | None = None
        try:
            if action == "claim_fixed":
                claim = self._claim(args)
                text, ok = "Claim recorded. The health check is running.", True
            elif action in self._handlers:
                text, ok = await self._handlers[action](args), True
            else:
                text, ok = f"unknown tool {action!r}. Available: {', '.join(TOOL_NAMES)}", False
        except ToolError as exc:
            text, ok = str(exc), False
        except Exception as exc:  # a tool bug must not end the incident
            text, ok = f"{action} failed: {type(exc).__name__}: {exc}", False
        text = clip(text)
        await self.ctx.db.add_action(self.incident_id, self.agent_id, step, action, args, ok, text)
        self.ctx.bus.publish(
            ToolCall(
                agent_id=self.agent_id, tool=action, args_summary=summarize_args(args), ok=ok,
                result_summary=first_line(text),
            )
        )
        return ToolResult(ok, text, claim if ok else None)

    # path jail ----------------------------------------------------------------------

    def _jail(self, rel: Any, base: Path | None = None) -> Path:
        base = (base or self.root).resolve()
        if not isinstance(rel, str) or not rel.strip():
            raise ToolError("a relative path is required")
        if "\x00" in rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise ToolError(f"path {rel!r} is outside the node directory; use paths relative to it")
        target = (base / rel).resolve()
        if target != base and base not in target.parents:
            raise ToolError(f"path {rel!r} is outside the node directory")
        return target

    # read-only tools ------------------------------------------------------------------

    async def _stats(self) -> dict[str, Any] | str:
        port = self.ctx.sup.port_of(self.node_id)
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                reply = await client.get(f"http://127.0.0.1:{port}/local/stats")
            if reply.status_code != 200:
                return f"HTTP {reply.status_code}"
            return reply.json()
        except httpx.ConnectError:
            return "connection refused"
        except (httpx.HTTPError, ValueError) as exc:
            return f"no valid answer ({type(exc).__name__})"

    async def node_status(self, args: dict[str, Any]) -> str:
        status: NodeStatus = self.ctx.sup.status(self.node_id)
        lines = [f"process: {status.describe()}", f"restarts so far: {status.restarts}"]
        stats = await self._stats()
        if isinstance(stats, str):
            lines.append(f"node stats endpoint on port {status.port}: {stats}")
        else:
            quota = stats.get("quota", {})
            lines.append(
                f"requests: total={stats.get('requests_total')} ok={stats.get('requests_ok')} "
                f"failed={stats.get('requests_failed')} by_status={stats.get('by_status')} in_flight={stats.get('in_flight')}"
            )
            lines.append(
                f"data quota: {quota.get('used_mb')} MB of {quota.get('limit_mb')} MB, over={quota.get('over')}; "
                f"worker_recycle_after={stats.get('worker_recycle_after')}; uptime {stats.get('uptime_s')}s"
            )
        return "\n".join(lines)

    async def tail_log(self, args: dict[str, Any]) -> str:
        try:
            n = int(args.get("n", 40))
        except (TypeError, ValueError):
            raise ToolError("n must be an integer") from None
        n = max(1, min(n, 80))
        lines = self.ctx.sup.tail_log(self.node_id, n)
        out = [f"logs/node.log (last {len(lines)} lines):", *lines] if lines else ["logs/node.log is empty"]
        if self.ctx.sup.status(self.node_id).state != "running":
            err = self.ctx.sup.stderr_tail(self.node_id, 20)
            if err:
                out += ["", "stderr.log (process is not running):", *err]
        return "\n".join(out)

    async def read_config(self, args: dict[str, Any]) -> str:
        rel = args.get("rel")
        target = self.paths.config if rel in (None, "", "config.yaml") else self._jail(rel)
        if target.is_dir():
            raise ToolError(f"{rel!r} is a directory; use list_dir")
        try:
            return target.read_text(errors="replace")[:MAX_FILE_CHARS]
        except OSError as exc:
            raise ToolError(f"cannot read {rel or 'config.yaml'}: {exc.strerror}") from None

    async def list_dir(self, args: dict[str, Any]) -> str:
        target = self._jail(args.get("rel", "."))
        if not target.is_dir():
            raise ToolError(f"{args.get('rel')!r} is not a directory")
        entries = []
        for entry in sorted(target.iterdir(), key=lambda p: p.name)[:200]:
            try:
                info = entry.lstat()
            except OSError:
                continue
            kind = "dir" if stat.S_ISDIR(info.st_mode) else "file"
            entries.append(f"{stat.filemode(info.st_mode)} {kind:4} {info.st_size:>10} {entry.name}")
        return "\n".join(entries) or "(empty)"

    async def port_owner(self, args: dict[str, Any]) -> str:
        port = self.ctx.sup.port_of(self.node_id)
        owner = await asyncio.to_thread(self.ctx.sup.port_owner, port)
        if owner is None:
            return f"nothing is listening on port {port}"
        tracked = "started by the fleet (can be removed with kill_squatter)" if owner["fleet_tracked"] else "not started by the fleet"
        return f"port {port} is held by pid {owner['pid']} ({owner['command']}), {tracked}"

    async def list_models(self, args: dict[str, Any]) -> str:
        base = settings.CRUSOE_BASE_URL
        cached = _models_cache.get(base)
        if cached is None or time.monotonic() - cached[0] > MODELS_TTL_S:
            key = os.environ.get(settings.API_KEY_ENV, "")
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    reply = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"})
                if reply.status_code != 200:
                    raise ToolError(f"upstream answered HTTP {reply.status_code} to GET {base}/models")
                ids = sorted(str(m.get("id")) for m in reply.json().get("data", []) if m.get("id"))
            except httpx.HTTPError as exc:
                raise ToolError(f"cannot reach {base}: {type(exc).__name__}") from None
            except ValueError:
                raise ToolError(f"{base}/models did not return JSON") from None
            _models_cache[base] = cached = (time.monotonic(), ids)
        return f"Upstream base URL: {base}\nModel ids (exact spelling, case-sensitive):\n" + "\n".join(cached[1][:40])

    async def probe_once(self, args: dict[str, Any]) -> str:
        probe = await self.ctx.referee.probe_once(self.node_id, "agent_probe")
        return f"probe {'ok' if probe.ok else 'FAILED'} in {probe.ms:.0f} ms: {probe.detail}"

    # changes --------------------------------------------------------------------------

    async def write_config(self, args: dict[str, Any]) -> str:
        text = args.get("yaml_text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("yaml_text is required")
        try:
            data = parse_config_text(text)
            cfg = NodeConfig(**data)
        except ConfigError as exc:
            raise ToolError(f"config rejected, not written: YAML error at {exc}") from None
        except ValidationError as exc:
            first = exc.errors()[0]
            raise ToolError(f"config rejected, not written: invalid {'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from None
        if cfg.node_id and cfg.node_id != self.node_id:
            raise ToolError(f"config rejected: node_id must stay {self.node_id!r}")
        current = self.ctx.sup.port_of(self.node_id)
        lo, hi = settings.SET_PORT_RANGE
        if cfg.port != current and not lo <= cfg.port <= hi:
            raise ToolError(f"config rejected: port {cfg.port} is not this node's port ({current}); use set_port for {lo}-{hi}")
        backup = self._backup()
        self.paths.config.write_text(text if text.endswith("\n") else text + "\n")
        kept = f" Previous file kept as {backup.name}." if backup else ""
        return f"config.yaml written ({len(text)} chars).{kept} Restart the node to apply it."

    def _backup(self) -> Path | None:
        if not self.paths.config.exists():
            return None
        backup = self.paths.config.with_name(f"config.yaml.bak-{int(time.time() * 1000)}")
        backup.write_bytes(self.paths.config.read_bytes())
        old = sorted(self.paths.config.parent.glob("config.yaml.bak-*"))[:-5]
        for path in old:
            with contextlib.suppress(OSError):
                path.unlink()
        return backup

    async def delete_files(self, args: dict[str, Any]) -> str:
        pattern = args.get("glob")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolError("glob is required, for example 'spool-*.bin'")
        pattern = pattern.strip()
        if pattern.startswith("/") or ".." in Path(pattern).parts or pattern.startswith("data/"):
            raise ToolError("glob is relative to data/ and may not leave it, for example 'spool-*.bin'")
        data = self.paths.data.resolve()
        if not data.is_dir():
            raise ToolError("data/ does not exist")
        newest = max(data.glob("ckpt-*.bin"), default=None, key=lambda p: p.name)
        matches = []
        for path in data.glob(pattern):
            if path.is_symlink() or not path.is_file():
                continue
            if data not in path.resolve().parents:
                continue
            if newest is not None and path.name == newest.name:
                continue
            matches.append(path)
        if not matches:
            return f"no files under data/ match {pattern!r} (the newest checkpoint is never deleted)"
        if len(matches) > MAX_DELETE:
            raise ToolError(f"{len(matches)} files match; narrow the glob")
        freed = 0
        for path in matches:
            freed += path.stat().st_size
            path.unlink()
        kept = f"; kept newest checkpoint {newest.name}" if newest is not None else ""
        return f"deleted {len(matches)} files ({freed / 1024 / 1024:.1f} MB){kept}"

    async def fix_permissions(self, args: dict[str, Any]) -> str:
        target = self._jail(args.get("rel"))
        if not target.exists():
            raise ToolError(f"{args.get('rel')!r} does not exist")
        before = stat.S_IMODE(target.stat().st_mode)
        after = 0o755 if target.is_dir() else 0o644
        os.chmod(target, after)
        return f"{args.get('rel')}: mode {before:04o} -> {after:04o}"

    async def _lifecycle(self, op: Callable[[str], Awaitable[NodeStatus]]) -> str:
        status = await op(self.node_id)
        out = [f"process: {status.describe()}"]
        if status.state != "running":
            err = self.ctx.sup.stderr_tail(self.node_id, 5)
            if err:
                out += ["stderr:", *err]
        return "\n".join(out)

    async def restart_node(self, args: dict[str, Any]) -> str:
        return await self._lifecycle(self.ctx.sup.restart)

    async def stop_node(self, args: dict[str, Any]) -> str:
        return await self._lifecycle(self.ctx.sup.stop)

    async def start_node(self, args: dict[str, Any]) -> str:
        return await self._lifecycle(self.ctx.sup.start)

    async def kill_squatter(self, args: dict[str, Any]) -> str:
        try:
            pid = int(args.get("pid"))
        except (TypeError, ValueError):
            raise ToolError("pid must be an integer") from None
        info = self.ctx.sup.squatters().get(pid)
        port = self.ctx.sup.port_of(self.node_id)
        if info is None or (info.get("node_id") != self.node_id and info.get("port") != port):
            raise ToolError(f"pid {pid} is not a process the fleet started on this node's port; refusing")
        if not await self.ctx.sup.kill_squatter(pid):
            raise ToolError(f"pid {pid} could not be killed")
        return f"killed pid {pid}; port {port} should be free. Start the node to bind it."

    async def set_port(self, args: dict[str, Any]) -> str:
        lo, hi = settings.SET_PORT_RANGE
        try:
            port = int(args.get("port"))
        except (TypeError, ValueError):
            raise ToolError("port must be an integer") from None
        if not lo <= port <= hi:
            raise ToolError(f"port must be in {lo}-{hi}")
        for node_id in self.ctx.sup.node_ids():
            if node_id != self.node_id and self.ctx.sup.port_of(node_id) == port:
                raise ToolError(f"port {port} belongs to {node_id}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise ToolError(f"port {port} is already in use")
        try:
            cfg = load_config_dict(self.paths.config)
        except (ConfigError, OSError) as exc:
            raise ToolError(f"config.yaml does not parse ({exc}); fix it with write_config first") from None
        cfg["port"] = port
        self._backup()
        self.paths.config.write_text(dump_config(cfg))
        return f"port set to {port} in config.yaml. Restart the node to apply it."

    def _claim(self, args: dict[str, Any]) -> dict[str, str]:
        summary = args.get("summary")
        cause = args.get("root_cause")
        if not isinstance(summary, str) or not summary.strip():
            raise ToolError("claim_fixed needs a summary of what you changed")
        if not isinstance(cause, str) or len(cause.strip()) < 8:
            raise ToolError("claim_fixed needs a root_cause that says why the node was failing")
        return {"summary": summary.strip(), "root_cause": cause.strip()}
