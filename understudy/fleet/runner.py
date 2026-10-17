"""Runs the fleet: starts every node, then the traffic generator and the monitor until stopped."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time

from config import settings
from understudy.db import Database
from understudy.events import EventBus
from understudy.fleet.monitor import Monitor
from understudy.fleet.referee import for_supervisor
from understudy.fleet.state import FleetState
from understudy.fleet.supervisor import Supervisor, pid_alive
from understudy.fleet.traffic import Traffic

log = logging.getLogger("understudy.fleet")

SNAPSHOT_PERSIST_S = 30.0
PID_FILE = settings.DATA_DIR / "fleet.pid"
LOG_FILE = settings.LOG_DIR / "fleet.log"


async def persist_events(bus: EventBus, db: Database) -> None:
    """Stores every event except most fleet snapshots, which arrive every couple of seconds, and ledgers, which
    are recomputed from the database on demand."""
    last_snapshot = 0.0
    with bus.subscribe(maxsize=5000) as sub:
        async for envelope in sub:
            if envelope.type == "ledger":
                continue
            if envelope.type == "fleet_snapshot":
                if envelope.ts - last_snapshot < SNAPSHOT_PERSIST_S:
                    continue
                last_snapshot = envelope.ts
            await db.add_event(envelope.ts, envelope.type, envelope.data)


async def run(keep_nodes: bool = False) -> None:
    db = await Database.open()
    bus = EventBus()
    sup = Supervisor()
    sup.ensure_all()
    started = await sup.start_all()
    for status in started:
        log.info("node %s: %s", status.node_id, status.describe())
    state = FleetState(settings.NODES, bus)
    referee = for_supervisor(sup)

    def mirror_proving_nodes() -> None:
        for node_id in sup.node_ids("proving"):
            if state.status(node_id) == "repairing":
                continue
            state.set_status(node_id, "healthy" if sup.status(node_id).state == "running" else "down")

    traffic = Traffic(state, sup.port_of, bus, refresh=mirror_proving_nodes)
    monitor = Monitor(state, referee, db, bus, sup)
    persister = asyncio.create_task(persist_events(bus, db))
    await traffic.start()
    await monitor.start()
    log.info("fleet running: %d nodes, monitor every %.0fs, traffic every %.0fs", len(started),
             settings.MONITOR_INTERVAL_S, settings.TRAFFIC_INTERVAL_S)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("stopping")
    await monitor.stop()
    await traffic.stop()
    persister.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await persister
    if not keep_nodes:
        await sup.stop_all()
    await db.close()
    log.info("stopped at %s", time.strftime("%H:%M:%S"))


def detach() -> int:
    """Starts the runner in its own session with output in data/logs/fleet.log and returns at once."""
    try:
        existing = int(PID_FILE.read_text())
    except (OSError, ValueError):
        existing = 0
    if existing and pid_alive(existing):
        print(f"fleet already running (pid {existing}); `make stop` ends it")
        return 1
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "understudy.fleet.runner"], cwd=settings.ROOT, stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid))
    print(f"fleet starting (pid {proc.pid}); log: {LOG_FILE.relative_to(settings.ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="understudy.fleet.runner")
    parser.add_argument("--keep-nodes", action="store_true", help="leave node processes running on exit")
    parser.add_argument("--detach", action="store_true", help="run in the background and return")
    args = parser.parse_args(argv)
    if args.detach:
        raise SystemExit(detach())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    PID_FILE.write_text(str(os.getpid()))
    try:
        asyncio.run(run(keep_nodes=args.keep_nodes))
    finally:
        with contextlib.suppress(OSError):
            PID_FILE.unlink()


if __name__ == "__main__":
    main()
