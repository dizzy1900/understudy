"""Probes live nodes and opens an incident after consecutive failures."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time

from config import settings
from understudy.db import Database
from understudy.events import EventBus, IncidentOpened
from understudy.fleet.referee import ProbeResult, Referee
from understudy.fleet.state import FleetState
from understudy.fleet.supervisor import Supervisor

log = logging.getLogger("understudy.monitor")

UNREACHABLE = ("connection refused", "connection closed")


class Monitor:
    def __init__(
        self,
        state: FleetState,
        referee: Referee,
        db: Database,
        bus: EventBus | None = None,
        supervisor: Supervisor | None = None,
        interval_s: float = settings.MONITOR_INTERVAL_S,
        failure_threshold: int = 2,
        rng: random.Random | None = None,
    ):
        self.state = state
        self.referee = referee
        self.db = db
        self.bus = bus
        self.supervisor = supervisor
        self.interval_s = interval_s
        self.failure_threshold = failure_threshold
        self.rng = rng or random.Random()
        self.failures: dict[str, int] = {}
        self._held: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []

    def reset(self, node_id: str) -> None:
        """Called when an incident closes so the next failure counts from zero."""
        self.failures[node_id] = 0

    def hold(self, node_id: str, seconds: float) -> None:
        """Keeps the node from opening another incident for a while, used after an incident ends unresolved."""
        self._held[node_id] = time.monotonic() + seconds
        self.failures[node_id] = 0

    def _symptom(self, node_id: str, probe: ProbeResult) -> tuple[str, str]:
        text = f"probe failed: {probe.detail}"
        unreachable = any(probe.detail.startswith(p) for p in UNREACHABLE)
        if unreachable and self.supervisor is not None:
            status = self.supervisor.status(node_id)
            if status.state == "exited":
                code = "unknown" if status.exit_code is None else status.exit_code
                text += f" (node process exited, code {code})"
        return text, "high" if unreachable else "low"

    async def check(self, node_id: str) -> ProbeResult:
        """One monitoring tick for one node."""
        probe = await self.referee.probe_once(node_id, "monitor")
        self.state.set_probe(node_id, probe.ms)
        if self.state.status(node_id) == "repairing":
            return probe
        if probe.ok:
            self.failures[node_id] = 0
            self.state.set_status(node_id, "healthy")
            return probe
        count = self.failures.get(node_id, 0) + 1
        self.failures[node_id] = count
        if count < self.failure_threshold:
            self.state.set_status(node_id, "degraded", probe.detail)
            return probe
        self.state.set_status(node_id, "down", probe.detail)
        if time.monotonic() < self._held.get(node_id, 0.0):
            return probe
        await self._open_incident(node_id, probe)
        return probe

    async def _open_incident(self, node_id: str, probe: ProbeResult) -> int | None:
        existing = await self.db.fetchone("SELECT id FROM incidents WHERE node_id = ? AND status = 'open'", (node_id,))
        if existing:
            return None
        symptom, severity = self._symptom(node_id, probe)
        incident_id = await self.db.open_incident(node_id, symptom, severity)
        await self.db.link_fault_incident(node_id, incident_id)
        log.info("incident %s opened on %s: %s", incident_id, node_id, symptom)
        if self.bus is not None:
            self.bus.publish(IncidentOpened(incident_id=incident_id, node_id=node_id, symptom=symptom, severity=severity))
        return incident_id

    async def _node_loop(self, node_id: str, offset: float) -> None:
        await asyncio.sleep(offset)
        while True:
            try:
                await self.check(node_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor tick for %s failed", node_id)
            await asyncio.sleep(self.interval_s * self.rng.uniform(0.9, 1.1))

    async def start(self) -> None:
        live = [n for n, v in self.state.nodes.items() if v.spec.zone == "live"]
        spread = self.interval_s / max(1, len(live))
        self._tasks = [asyncio.create_task(self._node_loop(n, i * spread)) for i, n in enumerate(live)]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
