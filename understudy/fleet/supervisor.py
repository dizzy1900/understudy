"""Starts, stops and restarts node processes and tracks the squatter processes it spawns.

State lives on disk (fleet/nodes/<id>/state.json) so a supervisor in one process and nodectl in another agree.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import httpx

from config import settings
from understudy.fleet.nodeconfig import (
    NodePaths,
    canonical_config,
    dump_config,
    read_port,
)

NodeState = Literal["running", "exited", "new"]
SQUATTERS_FILE = "squatters.json"


@dataclass
class NodeStatus:
    node_id: str
    state: NodeState
    pid: int | None
    port: int
    exit_code: int | None = None
    exit_reason: str | None = None
    started_at: float | None = None
    exited_at: float | None = None
    uptime_s: float | None = None
    restarts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        if self.state == "running":
            uptime = f" uptime={self.uptime_s:.0f}s" if self.uptime_s is not None else ""
            return f"running pid={self.pid} port={self.port}{uptime}"
        if self.state == "new":
            return f"not started port={self.port}"
        code = "unknown" if self.exit_code is None else self.exit_code
        reason = f" ({self.exit_reason})" if self.exit_reason else ""
        return f"exited code={code}{reason} port={self.port}"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _is_zombie(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return out.strip().startswith("Z")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


class Supervisor:
    def __init__(
        self,
        specs: Iterable[settings.NodeSpec] | None = None,
        nodes_dir: Path | None = None,
        python: str | None = None,
        model: str | None = None,
    ):
        self.nodes_dir = Path(nodes_dir) if nodes_dir else settings.NODES_DIR
        self.specs = {spec.id: spec for spec in (specs if specs is not None else settings.NODES)}
        self.python = python or sys.executable
        self.model = model
        self._procs: dict[str, subprocess.Popen] = {}
        self._squatter_procs: dict[int, subprocess.Popen] = {}

    # layout ---------------------------------------------------------------------

    def paths(self, node_id: str) -> NodePaths:
        return NodePaths(self.nodes_dir / node_id)

    def port_of(self, node_id: str) -> int:
        return read_port(self.paths(node_id).config, self.specs[node_id].port)

    def node_ids(self, zone: str | None = None) -> list[str]:
        return [s.id for s in self.specs.values() if zone is None or s.zone == zone]

    def ensure_node(self, node_id: str, overwrite_config: bool = False) -> NodePaths:
        spec = self.specs[node_id]
        paths = self.paths(node_id)
        paths.data.mkdir(parents=True, exist_ok=True)
        paths.log.parent.mkdir(parents=True, exist_ok=True)
        if overwrite_config or not paths.config.exists():
            paths.config.write_text(dump_config(canonical_config(spec.id, spec.port, self.model)))
        return paths

    def ensure_all(self) -> None:
        for node_id in self.specs:
            self.ensure_node(node_id)

    # status ---------------------------------------------------------------------

    def _alive(self, node_id: str, pid: int) -> tuple[bool, int | None]:
        proc = self._procs.get(node_id)
        if proc is not None and proc.pid == pid:
            code = proc.poll()
            return code is None, code
        return pid_alive(pid), None

    def status(self, node_id: str) -> NodeStatus:
        paths = self.paths(node_id)
        port = self.port_of(node_id)
        state = _read_json(paths.state)
        pid = state.get("pid")
        if not pid:
            return NodeStatus(node_id, "new", None, port, restarts=int(state.get("restarts", 0)))
        alive, code = self._alive(node_id, pid)
        started_at = state.get("started_at")
        if alive:
            return NodeStatus(
                node_id, "running", pid, port, started_at=started_at,
                uptime_s=time.time() - started_at if started_at else None, restarts=int(state.get("restarts", 0)),
            )
        changed = False
        if "exited_at" not in state:
            state["exited_at"] = time.time()
            changed = True
        info = _read_json(paths.exit_info)
        if code is None and info.get("pid") == pid:
            code = info.get("code")
            state.setdefault("exit_reason", info.get("reason"))
        if code is not None and state.get("exit_code") != code:
            state["exit_code"] = code
            changed = True
        if "exit_reason" not in state and info.get("pid") == pid and info.get("reason"):
            state["exit_reason"] = info["reason"]
            changed = True
        if changed:
            _write_json(paths.state, state)
        return NodeStatus(
            node_id, "exited", pid, port, exit_code=state.get("exit_code"), exit_reason=state.get("exit_reason"),
            started_at=started_at, exited_at=state.get("exited_at"), restarts=int(state.get("restarts", 0)),
        )

    def statuses(self, zone: str | None = None) -> list[NodeStatus]:
        return [self.status(node_id) for node_id in self.node_ids(zone)]

    # lifecycle ------------------------------------------------------------------

    def _argv(self, paths: NodePaths) -> list[str]:
        try:
            node_dir = str(paths.root.relative_to(settings.ROOT))
        except ValueError:
            node_dir = str(paths.root)
        return [self.python, "-m", "understudy.fleet.node_app", "--node-dir", node_dir]

    async def start(self, node_id: str, wait: bool = True, timeout: float = 20.0) -> NodeStatus:
        current = self.status(node_id)
        if current.state == "running":
            return current
        paths = self.ensure_node(node_id)
        with contextlib.suppress(OSError):
            paths.exit_info.unlink()
        # Each process gets a fresh stderr so lines from an earlier, already repaired fault do not read as current.
        with open(paths.stderr, "wb") as err:
            proc = subprocess.Popen(
                self._argv(paths), cwd=settings.ROOT, stdin=subprocess.DEVNULL, stdout=err, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._procs[node_id] = proc
        old = _read_json(paths.state)
        _write_json(paths.state, {"pid": proc.pid, "started_at": time.time(), "restarts": int(old.get("restarts", 0)) + 1})
        if wait:
            await self._wait_ready(node_id, proc, timeout)
        return self.status(node_id)

    async def _wait_ready(self, node_id: str, proc: subprocess.Popen, timeout: float) -> bool:
        url = f"http://127.0.0.1:{self.port_of(node_id)}/local/stats"
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=1.0) as client:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return False
                try:
                    reply = await client.get(url)
                    if reply.status_code == 200 and reply.json().get("pid") == proc.pid:
                        return True
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.1)
        return False

    async def _wait_dead(self, node_id: str, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._alive(node_id, pid)[0]:
                return True
            await asyncio.sleep(0.05)
        return False

    def _record_exit(self, node_id: str, code: int, reason: str) -> None:
        paths = self.paths(node_id)
        state = _read_json(paths.state)
        if state.get("exit_code") is None:
            state["exit_code"] = code
            state["exit_reason"] = reason
            _write_json(paths.state, state)

    async def stop(self, node_id: str, timeout: float = 5.0) -> NodeStatus:
        current = self.status(node_id)
        if current.state != "running" or current.pid is None:
            return current
        pid = current.pid
        sig = signal.SIGTERM
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, sig)
        if not await self._wait_dead(node_id, pid, timeout):
            sig = signal.SIGKILL
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
            await self._wait_dead(node_id, pid, 3.0)
        self._record_exit(node_id, -int(sig), "stopped by supervisor")
        return self.status(node_id)

    async def kill(self, node_id: str, sig: int = signal.SIGKILL) -> NodeStatus:
        current = self.status(node_id)
        if current.state != "running" or current.pid is None:
            return current
        with contextlib.suppress(ProcessLookupError):
            os.kill(current.pid, sig)
        await self._wait_dead(node_id, current.pid, 3.0)
        self._record_exit(node_id, -int(sig), f"killed by signal {int(sig)}")
        return self.status(node_id)

    async def restart(self, node_id: str, timeout: float = 20.0) -> NodeStatus:
        await self.stop(node_id)
        return await self.start(node_id, timeout=timeout)

    async def start_all(self, zone: str | None = None) -> list[NodeStatus]:
        self.ensure_all()
        return list(await asyncio.gather(*(self.start(n) for n in self.node_ids(zone))))

    async def stop_all(self, zone: str | None = None) -> list[NodeStatus]:
        return list(await asyncio.gather(*(self.stop(n) for n in self.node_ids(zone))))

    async def reset(self, node_id: str, start: bool = True) -> NodeStatus:
        """Returns the node to a known-good state: canonical config, empty data, fresh log, no squatter."""
        await self.stop(node_id)
        port = self.specs[node_id].port
        for pid, info in list(self.squatters().items()):
            if info.get("node_id") == node_id or info.get("port") in (port, self.port_of(node_id)):
                await self.kill_squatter(pid)
        paths = self.paths(node_id)
        if paths.log.exists():
            os.chmod(paths.log, 0o644)
        shutil.rmtree(paths.data, ignore_errors=True)
        for stale in (paths.exit_info, paths.stderr, paths.log):
            with contextlib.suppress(OSError):
                stale.unlink()
        paths.config.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_node(node_id, overwrite_config=True)
        with contextlib.suppress(OSError):
            paths.state.unlink()
        return await self.start(node_id) if start else self.status(node_id)

    async def reset_all(self, zone: str | None = None) -> list[NodeStatus]:
        return list(await asyncio.gather(*(self.reset(n) for n in self.node_ids(zone))))

    # logs -----------------------------------------------------------------------

    def tail_log(self, node_id: str, n: int = 80) -> list[str]:
        return _tail(self.paths(node_id).log, n)

    def stderr_tail(self, node_id: str, n: int = 40) -> list[str]:
        return _tail(self.paths(node_id).stderr, n)

    # squatters ------------------------------------------------------------------

    def _squatter_file(self) -> Path:
        return self.nodes_dir / SQUATTERS_FILE

    def squatters(self) -> dict[int, dict[str, Any]]:
        raw = _read_json(self._squatter_file())
        alive = {int(pid): info for pid, info in raw.items() if pid_alive(int(pid))}
        if len(alive) != len(raw):
            self._save_squatters(alive)
        return alive

    def _save_squatters(self, data: dict[int, dict[str, Any]]) -> None:
        self.nodes_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self._squatter_file(), {str(pid): info for pid, info in data.items()})

    async def spawn_squatter(self, node_id: str, ttl_s: float = 6 * 3600) -> int:
        port = self.port_of(node_id)
        proc = subprocess.Popen(
            [self.python, "-m", "understudy.fleet.supervisor", "squatter", "--port", str(port), "--ttl", str(ttl_s)],
            cwd=settings.ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._squatter_procs[proc.pid] = proc
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"squatter exited early with code {proc.returncode}: port {port} may already be in use")
            with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
            await asyncio.sleep(0.05)
        tracked = self.squatters()
        tracked[proc.pid] = {"node_id": node_id, "port": port, "started_at": time.time()}
        self._save_squatters(tracked)
        return proc.pid

    async def kill_squatter(self, pid: int) -> bool:
        """Kills a squatter this supervisor spawned. Refuses any other pid."""
        tracked = self.squatters()
        if pid not in tracked:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        proc = self._squatter_procs.pop(pid, None)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if proc is not None:
                if proc.poll() is not None:
                    break
            elif not pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        tracked.pop(pid, None)
        self._save_squatters(tracked)
        return True

    def port_owner(self, port: int) -> dict[str, Any] | None:
        try:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"], capture_output=True, text=True, timeout=5
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        pid = command = None
        for line in out.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("c") and pid is not None and command is None:
                command = line[1:]
        if pid is None:
            return None
        return {"pid": pid, "command": command, "port": port, "fleet_tracked": pid in self.squatters()}


def _tail(path: Path, n: int) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 64 * 1024))
            data = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return [f"cannot read {path.name}: {exc.strerror}"]
    return data.splitlines()[-n:]


def run_squatter(port: int, ttl_s: float) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # A port the node just released can still be in TIME_WAIT; a listening socket on it still blocks a second bind.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(8)
    sock.settimeout(1.0)
    deadline = time.monotonic() + ttl_s
    while time.monotonic() < deadline:
        try:
            conn, _addr = sock.accept()
        except socket.timeout:
            continue
        conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="understudy.fleet.supervisor")
    sub = parser.add_subparsers(dest="cmd", required=True)
    squat = sub.add_parser("squatter", help="hold a port open (used by the port_squatter fault)")
    squat.add_argument("--port", type=int, required=True)
    squat.add_argument("--ttl", type=float, default=6 * 3600)
    args = parser.parse_args(argv)
    if args.cmd == "squatter":
        run_squatter(args.port, args.ttl)


if __name__ == "__main__":
    main()
