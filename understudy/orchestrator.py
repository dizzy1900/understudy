"""Process entry point that wires the monitor, agents and event bus together."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import time

import uvicorn

from config import settings
from understudy import ledger, llm
from understudy.agents.chaos import ChaosOps
from understudy.agents.commander import Commander
from understudy.agents.librarian import Librarian
from understudy.agents.narrator import Narrator
from understudy.agents.promoter import Promoter
from understudy.agents.verifier import Verifier
from understudy.context import BudgetGate, Context
from understudy.db import Database
from understudy.events import EventBus
from understudy.fleet.monitor import Monitor
from understudy.fleet.referee import for_supervisor
from understudy.fleet.runner import PID_FILE as FLEET_PID_FILE
from understudy.fleet.runner import persist_events
from understudy.fleet.state import FleetState
from understudy.fleet.supervisor import Supervisor, pid_alive
from understudy.fleet.traffic import Traffic
from understudy.server import create_app

log = logging.getLogger("understudy.orchestrator")

PID_FILE = settings.DATA_DIR / "orchestrator.pid"
LOG_FILE = settings.LOG_DIR / "orchestrator.log"
LEDGER_INTERVAL_S = 3.0
HEARTBEAT_INTERVAL_S = 60.0


class Orchestrator:
    def __init__(self, autopilot: bool = False, serve: bool = True, host: str = "127.0.0.1", port: int = settings.SERVER_PORT):
        self.autopilot = autopilot
        self.serve = serve
        self.host = host
        self.port = port
        self._tasks: list[asyncio.Task] = []
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self.started_at = time.time()

    async def start(self) -> None:
        settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.db = await Database.open()
        await ledger.ledger_since(self.db)
        self.bus = EventBus()
        self.sup = Supervisor()
        self.sup.ensure_all()
        started = await self.sup.start_all()
        log.info("nodes: %d running of %d", sum(1 for s in started if s.state == "running"), len(started))
        self.state = FleetState(settings.NODES, self.bus)
        referee = for_supervisor(self.sup)
        self.client = llm.configure(self.db)

        self.ctx = Context(
            self.db, self.bus, self.sup, self.state, referee, chat=llm.chat, resolve=self.client.resolve,
            gate=BudgetGate(self.client.spent, self.client.budget),
        )
        self.ctx.gate.say = lambda text, priority: self.ctx.say(text, priority, polish=False)
        self.ctx.verifier = Verifier(self.ctx)
        self.ctx.narrator = Narrator(self.ctx)
        self.librarian = self.ctx.librarian = Librarian(self.ctx)
        self.promoter = self.ctx.promoter = Promoter(self.ctx)

        self.traffic = Traffic(self.state, self.sup.port_of, self.bus, refresh=self._mirror_proving_nodes)
        self.monitor = Monitor(self.state, referee, self.db, self.bus, self.sup)
        self.ctx.monitor = self.monitor
        self.commander = Commander(self.ctx)
        self.chaos = ChaosOps(self.ctx)

        self._tasks = [asyncio.create_task(persist_events(self.bus, self.db))]
        await self.commander.start()
        await self.traffic.start()
        await self.monitor.start()
        await self.promoter.start(loops=self.autopilot)
        self._tasks.append(self.ctx.background(self.librarian.backfill()))
        self._tasks += [asyncio.create_task(self._ledger_loop()), asyncio.create_task(self._heartbeat_loop())]
        if self.autopilot:
            self.chaos.start_autopilot()
        if self.serve:
            app = create_app(self)
            config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning", lifespan="off")
            self._server = uvicorn.Server(config)
            self._server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            self._server_task = asyncio.create_task(self._server.serve())
        log.info(
            "orchestrator running: autopilot=%s serve=%s budget=$%s", self.autopilot, self.serve, self.client.budget
        )

    def _mirror_proving_nodes(self) -> None:
        for node_id in self.sup.node_ids("proving"):
            if self.state.status(node_id) == "repairing":
                continue
            self.state.set_status(node_id, "healthy" if self.sup.status(node_id).state == "running" else "down")

    async def stop(self) -> None:
        log.info("stopping")
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._server_task, 5.0)
        await self.chaos.stop()
        await self.promoter.stop()
        await self.librarian.stop()
        await self.commander.stop()
        await self.monitor.stop()
        await self.traffic.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.client.aclose()
        await self.db.close()

    # periodic work --------------------------------------------------------------------

    async def _ledger_loop(self) -> None:
        while True:
            try:
                self.bus.publish(await ledger.compute(self.db))
            except Exception:
                log.exception("ledger update failed")
            await asyncio.sleep(LEDGER_INTERVAL_S)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            try:
                log.info(await self.heartbeat())
            except Exception:
                log.exception("heartbeat failed")

    async def heartbeat(self) -> str:
        led = await ledger.compute(self.db)
        live = [v for v in self.state.nodes.values() if v.spec.zone == "live"]
        healthy = sum(1 for v in live if v.status == "healthy")
        open_now = len(await self.db.open_incidents())
        unresolved = await self.db.scalar("SELECT COUNT(*) FROM incidents WHERE status = 'unresolved'")
        return (
            f"heartbeat up={time.time() - self.started_at:.0f}s nodes_healthy={healthy}/{len(live)} open={open_now} "
            f"incidents={led.incidents_total} verified={led.verified} unresolved={unresolved} "
            f"rejected_claims={led.rejected_claims} cost=${led.cost_total_usd:.4f} cache_hit={led.cache_hit_rate:.0%} "
            f"tokens_served={led.tokens_served} paused={self.ctx.gate.paused}"
        )

    # demo -----------------------------------------------------------------------------

    async def reset_demo(self) -> None:
        """Returns the fleet and the learning state to day zero. Spend stays in llm_calls."""
        for task in list(self.commander.active.values()):
            task.cancel()
        await asyncio.sleep(0.2)
        for table in (
            "incidents", "actions", "verifications", "faults_truth", "events", "ladder_events", "heldout_reports", "skills",
        ):
            await self.db.execute(f"DELETE FROM {table}")
        await self.db.execute("UPDATE ladder SET rung = 'super', updated_at = ?", (time.time(),))
        await self.db.delete_setting("away_since")
        await self.db.delete_setting("promoter_last_run")
        await self.db.delete_setting("heldout_last_run")
        shutil.rmtree(self.ctx.scripts_dir, ignore_errors=True)
        await self.db.set_setting("ledger_since", repr(time.time()))
        await self.sup.reset_all()
        for node_id in self.state.nodes:
            self.state.set_status(node_id, "healthy")
            self.monitor.reset(node_id)


async def run(autopilot: bool, serve: bool, port: int) -> None:
    orch = Orchestrator(autopilot=autopilot, serve=serve, port=port)
    await orch.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await orch.stop()


def detach(args: list[str]) -> int:
    """Starts the orchestrator in its own session with output in data/logs/orchestrator.log and returns at once."""
    for pid_file, name in ((PID_FILE, "orchestrator"), (FLEET_PID_FILE, "fleet runner")):
        try:
            existing = int(pid_file.read_text())
        except (OSError, ValueError):
            continue
        if pid_alive(existing):
            print(f"{name} already running (pid {existing}); `make stop` ends it")
            return 1
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "understudy.orchestrator", *args], cwd=settings.ROOT, stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid))
    print(f"orchestrator starting (pid {proc.pid}); log: {LOG_FILE.relative_to(settings.ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="understudy.orchestrator")
    parser.add_argument("--autopilot", action="store_true", help="inject faults on a schedule")
    parser.add_argument("--no-server", action="store_true", help="do not serve the UI API")
    parser.add_argument("--port", type=int, default=settings.SERVER_PORT)
    parser.add_argument("--detach", action="store_true", help="run in the background and return")
    args = parser.parse_args(argv)
    if args.detach:
        forward = [a for a in (argv if argv is not None else sys.argv[1:]) if a != "--detach"]
        raise SystemExit(detach(forward))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if FLEET_PID_FILE.exists():
        with contextlib.suppress(OSError, ValueError):
            if pid_alive(int(FLEET_PID_FILE.read_text())):
                raise SystemExit("the fleet runner is running and has its own monitor; stop it with `make stop` first")
    PID_FILE.write_text(str(os.getpid()))
    try:
        asyncio.run(run(args.autopilot, not args.no_server, args.port))
    finally:
        with contextlib.suppress(OSError):
            if PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()


if __name__ == "__main__":
    main()
