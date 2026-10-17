"""Standalone mock of the event stream for UI work.

Serves the /ws and /api/* contract on :8765 with synthetic data. It does not
touch the fleet, the database or Crusoe, and its numbers are not measurements.
Set MOCK_SPEED to run the story faster than real time.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from understudy import events as ev

log = logging.getLogger("mock_events")

RUNGS = settings.RUNGS
MAX_DOWN = 6
REJECT_EVERY_S = 60
STORM_EVERY_S = 240
PROMOTER_EVERY_S = 45
PROVING_TRIALS = 5

COST_RANGE = {"super": (0.020, 0.050), "lightning": (0.003, 0.008), "script": (0.0004, 0.0010)}
HIGH_SEVERITY = {"port_squatter", "crashed_worker", "config_syntax", "log_permission"}


@dataclass(frozen=True)
class Fault:
    symptom: str
    probe_error: str
    pitfall: str
    claim: str
    # (thought, tool, args_summary, result_summary)
    steps: tuple[tuple[str, str, str, str], ...]
    fails_at: str = "probe_a"


FAULTS: dict[str, Fault] = {
    "model_id_case": Fault(
        symptom="upstream 404 model_not_found",
        probe_error="HTTP 404 model_not_found",
        pitfall="model ids are case-sensitive; copy the id from list_models",
        claim="Corrected the model id casing and restarted; probe returns 200",
        steps=(
            ("Probe returns 404 model_not_found. Check which model id goes upstream.", "tail_log", "n=40",
             "upstream 404: model 'nvidia/nemotron-3-nano-30b-a3b' not found"),
            ("Compare with the ids the API lists.", "list_models", "", "canonical id nvidia/Nemotron-3-Nano-30B-A3B"),
            ("Casing differs. Fix the model id in the config.", "write_config", "model: nvidia/Nemotron-3-Nano-30B-A3B",
             "config written"),
            ("Restart so the node reads the new config.", "restart_node", "", "node up"),
            ("One probe to confirm before claiming.", "probe_once", "", "200 OK, model matches"),
        ),
    ),
    "stale_endpoint": Fault(
        symptom="upstream host unreachable",
        probe_error="502 upstream connect error",
        pitfall="check the hostname, not only the path",
        claim="Pointed upstream_base_url at the current hostname and restarted",
        steps=(
            ("Probe fails at connect. Look for the upstream error.", "tail_log", "n=40",
             "ConnectError: api.inference.crusoe.ai name not resolved"),
            ("That hostname looks outdated. Read the config.", "read_config", "", "upstream_base_url: https://api.inference.crusoe.ai/v1"),
            ("Set the current endpoint.", "write_config", "upstream_base_url: https://api.inference.crusoecloud.com/v1",
             "config written"),
            ("Restart the node.", "restart_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "starved_tokens": Fault(
        symptom="empty content, finish_reason=length",
        probe_error="200 but empty content",
        pitfall="thinking spends max_tokens; keep at least 2048 when it is on",
        claim="Raised max_tokens so reasoning no longer starves the answer",
        steps=(
            ("200 responses with empty content usually mean the token budget ran out.", "tail_log", "n=40",
             "finish_reason=length completion_tokens=16 content=''"),
            ("Read the token settings.", "read_config", "", "max_tokens: 16, enable_thinking: true"),
            ("Raise max_tokens well above the reasoning cost.", "write_config", "max_tokens: 2048", "config written"),
            ("Restart the node.", "restart_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK, content 'OK'"),
        ),
    ),
    "forbidden_param": Fault(
        symptom="403 parameter not allowed",
        probe_error="HTTP 403 parameter 'thinking' is not allowed",
        pitfall="remove the whole thinking key, not just its value",
        claim="Removed the top-level thinking parameter from extra_body",
        steps=(
            ("403 from upstream. Read the error body in the log.", "tail_log", "n=40",
             "403 Request blocked: parameter 'thinking' is not allowed"),
            ("Find where the parameter is set.", "read_config", "", "extra_body: {thinking: {type: enabled}}"),
            ("Drop it and use chat_template_kwargs only.", "write_config", "extra_body: chat_template_kwargs",
             "config written"),
            ("Restart the node.", "restart_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "bad_key_env": Fault(
        symptom="401 invalid api key",
        probe_error="HTTP 401 Unauthorized",
        pitfall="the variable has to exist in the node's environment, not only the shell",
        claim="Pointed api_key_env at the variable that is set and restarted",
        steps=(
            ("401 means the key did not reach upstream. Check the log.", "tail_log", "n=40",
             "401 Unauthorized; env var CRUSOE_KEY is not set"),
            ("See which variable the config names.", "read_config", "", "api_key_env: CRUSOE_KEY"),
            ("Use the variable the fleet actually exports.", "write_config", "api_key_env: CRUSOE_API_KEY",
             "config written"),
            ("Restart the node.", "restart_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "port_squatter": Fault(
        symptom="node cannot bind its port",
        probe_error="connection refused",
        pitfall="kill only fleet-tracked pids; otherwise move the node to another port",
        claim="Killed the squatter holding the port and started the node",
        steps=(
            ("Node is not answering. Look at the boot log.", "tail_log", "n=40", "OSError: address already in use :9204"),
            ("Who holds the port?", "port_owner", "", "pid 48213 (fleet dummy)"),
            ("It is fleet-tracked, so it is safe to kill.", "kill_squatter", "pid=48213", "killed"),
            ("Start the node.", "start_node", "", "node up on :9204"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "disk_full": Fault(
        symptom="507 quota exceeded",
        probe_error="HTTP 507 quota exceeded",
        pitfall="keep the newest checkpoint when clearing data/",
        claim="Cleared old checkpoints under data/ and kept the newest",
        steps=(
            ("507 points at storage. Check the log.", "tail_log", "n=40", "507 quota exceeded: data/ 1.2 GB of 1.0 GB"),
            ("Look at what fills data/.", "list_dir", "data", "412 checkpoint files"),
            ("Delete old checkpoints, keeping the newest.", "delete_files", "data/ckpt-*.bin", "deleted 411 files"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "crashed_worker": Fault(
        symptom="node process not running",
        probe_error="connection refused",
        pitfall="a dead process leaves no error in the log; check node_status first",
        claim="Started the node process again; probe returns 200",
        steps=(
            ("Connection refused. Is the process alive?", "node_status", "", "state=exited code=-9"),
            ("Killed by signal. Check for a config problem before restarting.", "tail_log", "n=40", "no errors before exit"),
            ("Config is fine. Start the node.", "start_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "config_syntax": Fault(
        symptom="node fails to boot, YAML error",
        probe_error="connection refused",
        pitfall="read the line number in the log before editing",
        claim="Fixed the YAML indentation on line 7 and started the node",
        steps=(
            ("Node does not boot. Read the error.", "tail_log", "n=40", "yaml.scanner.ScannerError: line 7, column 3"),
            ("Open the config around line 7.", "read_config", "", "line 7: mis-indented max_tokens"),
            ("Fix the indentation.", "write_config", "max_tokens: 2048 (indent fixed)", "config written"),
            ("Start the node.", "start_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "leaky_worker": Fault(
        symptom="worker exits after a few requests",
        probe_error="burst failed after 2 of 4 requests",
        pitfall="a plain restart passes the first probe and fails again within a few requests",
        claim="Disabled the worker recycle limit that made the worker exit",
        fails_at="burst",
        steps=(
            ("Node answers, then drops. Look for worker exits.", "tail_log", "n=80",
             "worker recycled after 3 requests; process exited"),
            ("Check the worker settings.", "read_config", "", "worker_recycle_after: 3"),
            ("A restart alone will repeat this. Remove the recycle limit.", "write_config", "worker_recycle_after: 0",
             "config written"),
            ("Restart the node.", "restart_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "log_permission": Fault(
        symptom="node cannot start, log not writable",
        probe_error="connection refused",
        pitfall="fix the file mode instead of redirecting logs",
        claim="Restored write permission on logs/node.log and started the node",
        steps=(
            ("Node will not start. Check the boot log.", "tail_log", "n=40", "PermissionError: logs/node.log"),
            ("Look at the log directory.", "list_dir", "logs", "node.log mode 000"),
            ("Restore the mode.", "fix_permissions", "logs/node.log", "mode 644"),
            ("Start the node.", "start_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
    "upstream_storm": Fault(
        symptom="429 storm from upstream",
        probe_error="HTTP 429 too many requests",
        pitfall="lower concurrency and add backoff; retries alone make the storm worse",
        claim="Lowered worker concurrency and enabled backoff",
        steps=(
            ("Many 429s in a short window. Check the rate.", "tail_log", "n=80", "429 x38 in 10s; concurrency=64"),
            ("Read the worker settings.", "read_config", "", "worker_concurrency: 64, backoff: false"),
            ("Reduce concurrency and turn backoff on.", "write_config", "worker_concurrency: 4, backoff: true",
             "config written"),
            ("Restart the node.", "restart_node", "", "node up"),
            ("Probe once.", "probe_once", "", "200 OK"),
        ),
    ),
}

CARD_SUMMARIES = (
    "Added pitfall from a verified fix",
    "Tightened fix steps",
    "Merged duplicate pitfalls",
)


def model_for(rung: str) -> str:
    return "fix_script" if rung == "script" else settings.MODELS[rung].id


@dataclass
class NodeSim:
    spec: settings.NodeSpec
    status: str = "healthy"
    probe_ms: float = 300.0
    busy: bool = False
    next_request: float = 0.0
    window: deque = field(default_factory=deque)

    def record(self, now: float, tokens: int) -> None:
        self.window.append((now, tokens))

    def tokens_per_min(self, now: float) -> float:
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        return float(sum(t for _, t in self.window))


class Sim:
    def __init__(self, speed: float = 1.0, seed: int = 7):
        self.speed = speed
        self.seed = seed
        self.bus = ev.EventBus()
        self.tasks: set[asyncio.Task] = set()
        self.promote_lock = asyncio.Lock()
        self._reset_state()

    def _reset_state(self) -> None:
        self.rng = random.Random(self.seed)
        self.nodes = {spec.id: NodeSim(spec, next_request=self.rng.uniform(0, 10)) for spec in settings.NODES}
        self.since = time.time()
        self.history: deque[ev.Envelope] = deque(maxlen=2000)
        self.seq = 0
        self.ladder: dict[str, str] = {c: "super" for c in settings.TRAINING_FAULT_CLASSES}
        self.ladder_log: list[dict] = []
        self.skills: dict[str, dict] = {}
        self.counts = {"incidents": 0, "closed": 0, "verified": 0, "rejected": 0, "promotions": 0, "demotions": 0}
        self.cost_incidents = 0.0
        self.cost_other = 0.0
        self.tokens_served = 0
        self.prompt_tokens = 0
        self.cached_tokens = 0
        self.by_rung = {r: ev.RungStat() for r in RUNGS}
        self.rung_started = {r: 0 for r in RUNGS}
        self.promoter_cycles = 0
        self.away: dict | None = None
        self.next_rejection = self.vnow() + REJECT_EVERY_S

    # time ------------------------------------------------------------------

    def vnow(self) -> float:
        """Story clock in seconds; runs `speed` times faster than the wall clock."""
        return time.monotonic() * self.speed

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)

    async def pause(self, low: float, high: float) -> None:
        await self.sleep(self.rng.uniform(low, high))

    # plumbing ----------------------------------------------------------------

    def emit(self, payload: ev.EventData) -> None:
        self.history.append(self.bus.publish(payload))

    def next_id(self) -> int:
        self.seq += 1
        return self.seq

    def run(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("task failed", exc_info=task.exception())

    def start(self) -> None:
        for loop in (self._ticker, self._traffic, self._scenario, self._promoter):
            self.run(loop())

    async def stop(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()

    async def reset(self) -> None:
        await self.stop()
        self._reset_state()
        self.start()
        self.emit(self.snapshot())
        self.emit(self.ledger())

    # state views ---------------------------------------------------------------

    def snapshot(self) -> ev.FleetSnapshot:
        now = self.vnow()
        return ev.FleetSnapshot(
            nodes=[
                ev.NodeInfo(
                    id=n.spec.id,
                    row=n.spec.row,
                    col=n.spec.col,
                    zone=n.spec.zone,
                    status=n.status,
                    tokens_per_min=n.tokens_per_min(now),
                    last_probe_ms=round(n.probe_ms, 1),
                )
                for n in self.nodes.values()
            ]
        )

    def ledger(self) -> ev.Ledger:
        closed = self.counts["closed"]
        return ev.Ledger(
            since=self.since,
            incidents_total=self.counts["incidents"],
            verified=self.counts["verified"],
            rejected_claims=self.counts["rejected"],
            promotions=self.counts["promotions"],
            demotions=self.counts["demotions"],
            cost_total_usd=round(self.cost_incidents + self.cost_other, 4),
            cost_per_incident_usd=round(self.cost_incidents / closed, 4) if closed else 0.0,
            tokens_served=self.tokens_served,
            cache_hit_rate=round(self.cached_tokens / self.prompt_tokens, 3) if self.prompt_tokens else 0.0,
            by_rung={r: s.model_copy() for r, s in self.by_rung.items()},
            ladder=dict(self.ladder),
        )

    def down_count(self) -> int:
        return sum(1 for n in self.nodes.values() if n.spec.zone == "live" and n.status != "healthy")

    def set_status(self, node_id: str, status: str, reason: str | None = None) -> None:
        self.nodes[node_id].status = status
        self.emit(ev.NodeStatus(node_id=node_id, status=status, reason=reason))

    def sample_cost(self, rung: str) -> float:
        return self.rng.uniform(*COST_RANGE[rung])

    # agents ----------------------------------------------------------------------

    def spawn_agent(self, role: str, rung: str, *, model: str | None = None, prefix: str | None = None,
                    incident_id: int | None = None, node_id: str | None = None) -> str:
        agent_id = f"{prefix or role}-{self.next_id()}"
        self.emit(ev.AgentSpawned(agent_id=agent_id, role=role, model=model or model_for(rung), rung=rung,
                                  incident_id=incident_id, node_id=node_id))
        return agent_id

    async def short_agent(self, role: str, rung: str, thought: str, **kwargs) -> None:
        agent = self.spawn_agent(role, rung, **kwargs)
        await self.pause(0.4, 1.0)
        self.emit(ev.AgentThought(agent_id=agent, text=thought))
        await self.pause(0.4, 1.0)
        self.emit(ev.AgentDone(agent_id=agent))

    async def narrate(self, text: str, priority: str = "low", incident_id: int | None = None) -> None:
        agent = self.spawn_agent("narrator", "nano", incident_id=incident_id)
        await self.pause(0.3, 0.8)
        self.emit(ev.Narration(text=text, priority=priority))
        self.emit(ev.AgentDone(agent_id=agent))

    # background loops --------------------------------------------------------------

    async def _ticker(self) -> None:
        tick = 0
        while True:
            await self.sleep(2)
            tick += 1
            self.emit(self.ledger())
            if tick % 2 == 0:
                self.emit(self.snapshot())

    async def _traffic(self) -> None:
        while True:
            now = self.vnow()
            for node in self.nodes.values():
                if node.spec.zone != "live" or node.status != "healthy" or now < node.next_request:
                    continue
                node.next_request = now + self.rng.uniform(8, 12)
                tokens = self.rng.randint(18, 90)
                node.record(now, tokens)
                node.probe_ms = self.rng.uniform(220, 1400)
                self.tokens_served += tokens
                self.emit(ev.Tokens(node_id=node.spec.id, tokens=tokens))
            await self.sleep(1)

    async def _scenario(self) -> None:
        await self.sleep(2)
        last_storm = self.vnow() - (STORM_EVERY_S - 60)
        while True:
            if self.vnow() - last_storm >= STORM_EVERY_S:
                last_storm = self.vnow()
                self.storm(None)
            elif self.down_count() < MAX_DOWN:
                free = self.free_live_nodes()
                if free:
                    self.inject(self.rng.choice(free), self.rng.choice(settings.TRAINING_FAULT_CLASSES))
            await self.pause(10, 25)

    async def _promoter(self) -> None:
        await self.sleep(25)
        while True:
            if not self.promote_lock.locked():
                await self.promote_cycle()
            await self.sleep(PROMOTER_EVERY_S)

    # injection ------------------------------------------------------------------------

    def free_live_nodes(self, rows: list[int] | None = None) -> list[str]:
        return [
            n.spec.id
            for n in self.nodes.values()
            if n.spec.zone == "live" and n.status == "healthy" and not n.busy and (rows is None or n.spec.row in rows)
        ]

    def inject(self, node_id: str, fault_class: str, storm: bool = False) -> bool:
        node = self.nodes[node_id]
        if node.spec.zone != "live" or node.busy or node.status != "healthy":
            return False
        node.busy = True
        self.run(self.run_incident(node_id, fault_class, storm))
        return True

    def storm(self, rows: list[int] | None) -> list[dict]:
        candidates = self.free_live_nodes(rows)
        room = MAX_DOWN - self.down_count()
        count = min(self.rng.randint(3, 6), len(candidates), max(room, 0))
        picked = self.rng.sample(candidates, count)
        injected = []
        for node_id in picked:
            fault_class = self.rng.choice(settings.TRAINING_FAULT_CLASSES)
            if self.inject(node_id, fault_class, storm=True):
                injected.append({"node_id": node_id, "fault_class": fault_class})
        if injected:
            self.run(self.narrate(f"Storm: {len(injected)} nodes failing at once. Responders are being dispatched.", "high"))
        return injected

    # incidents ---------------------------------------------------------------------------

    def take_rejection(self) -> bool:
        if self.vnow() < self.next_rejection:
            return False
        self.next_rejection = self.vnow() + REJECT_EVERY_S
        return True

    async def run_incident(self, node_id: str, fault_class: str, storm: bool) -> None:
        try:
            await self._incident(node_id, fault_class, storm)
        finally:
            self.nodes[node_id].busy = False

    async def _incident(self, node_id: str, fault_class: str, storm: bool) -> None:
        fault = FAULTS[fault_class]
        inc = self.next_id()
        started = self.vnow()

        self.set_status(node_id, "degraded", fault.symptom)
        await self.pause(3, 5)
        self.set_status(node_id, "down", fault.symptom)
        self.counts["incidents"] += 1
        severity = "high" if storm or fault_class in HIGH_SEVERITY else "low"
        self.emit(ev.IncidentOpened(incident_id=inc, node_id=node_id, symptom=fault.symptom, severity=severity))

        rung = self.ladder.get(fault_class, "super")
        cost = 0.0006 + 0.004 + 0.005  # triage, commander, grader
        await self.short_agent("triage", "nano", f"Log and probe evidence match {fault_class}.",
                               incident_id=inc, node_id=node_id)
        await self.short_agent("commander", "super", f"{fault_class} is on the {rung} rung. Spawning a {rung} responder.",
                               incident_id=inc, node_id=node_id)
        await self.narrate(f"{node_id} is down: {fault.symptom}. A {rung} responder is on it.",
                           "high" if storm else "low", inc)
        self.set_status(node_id, "repairing")

        steps = 0
        current = rung
        escalating = current != "super" and self.rung_started[current] % 4 == 3
        self.rung_started[current] += 1
        while True:
            agent = self.spawn_agent("responder", current, incident_id=inc, node_id=node_id)
            plan = fault.steps[:3] if escalating else fault.steps
            for thought, tool, args, result in plan:
                if current == "script":
                    await self.pause(0.3, 0.8)
                else:
                    self.emit(ev.AgentThought(agent_id=agent, text=thought))
                    await self.pause(1.5, 3.5)
                self.emit(ev.ToolCall(agent_id=agent, tool=tool, args_summary=args, ok=True, result_summary=result))
                steps += 1
            if not escalating:
                break
            cost += self.sample_cost(current) * 0.4
            watchdog = self.spawn_agent("watchdog", current, model="code", incident_id=inc, node_id=node_id)
            higher = RUNGS[RUNGS.index(current) - 1]
            reason = "repeated action 3x without progress"
            self.emit(ev.AgentThought(agent_id=watchdog, text=f"{current} responder is looping. Escalating to {higher}."))
            self.emit(ev.Escalation(incident_id=inc, from_rung=current, to_rung=higher, reason=reason))
            self.emit(ev.AgentDone(agent_id=watchdog))
            self.emit(ev.AgentDone(agent_id=agent))
            await self.narrate(f"{node_id}: {current} responder stalled, escalating to {higher}.", "high", inc)
            current, escalating = higher, False

        cost += self.sample_cost(current)
        quality = await self.verify_loop(inc, node_id, fault, agent, current)
        steps += 1  # claim_fixed
        self.set_status(node_id, "healthy")
        self.emit(ev.AgentDone(agent_id=agent))

        prompt = self.rng.randint(5000, 9000)
        self.prompt_tokens += prompt
        self.cached_tokens += 0 if self.counts["closed"] == 0 else int(prompt * self.rng.uniform(0.5, 0.9))
        self.counts["verified"] += 1
        self.counts["closed"] += 1
        self.cost_incidents += cost
        self.by_rung[current].incidents += 1
        self.by_rung[current].cost_usd = round(self.by_rung[current].cost_usd + cost, 6)
        self.emit(ev.IncidentClosed(incident_id=inc, node_id=node_id, fault_class=fault_class, rung=current,
                                    steps=steps, cost_usd=round(cost, 5), seconds=round(self.vnow() - started, 1)))

        if quality >= 4 and fault_class in settings.TRAINING_FAULT_CLASSES:
            await self.maybe_update_skill(fault_class, fault)

    async def verify_loop(self, inc: int, node_id: str, fault: Fault, agent: str, rung: str) -> int:
        while True:
            self.emit(ev.ToolCall(agent_id=agent, tool="claim_fixed", args_summary=fault.claim[:60], ok=True,
                                  result_summary="referee running"))
            self.emit(ev.FixClaimed(incident_id=inc, agent_id=agent, summary=fault.claim))
            referee = self.spawn_agent("verifier", "script", model="referee", incident_id=inc, node_id=node_id)
            await self.pause(8, 10)  # probe A, 8 s gap, burst of 4, probe B

            if self.take_rejection():
                self.counts["rejected"] += 1
                probes = [ev.Probe(name="probe_a", ok=fault.fails_at != "probe_a", ms=self.rng.uniform(200, 900),
                                   detail="200 OK" if fault.fails_at != "probe_a" else fault.probe_error)]
                if fault.fails_at == "burst":
                    probes.append(ev.Probe(name="burst", ok=False, ms=self.rng.uniform(900, 2500), detail=fault.probe_error))
                self.emit(ev.Verification(incident_id=inc, node_id=node_id, verdict="rejected", probes=probes,
                                          note="Claim disproved by the referee; evidence returned to the responder."))
                self.emit(ev.AgentDone(agent_id=referee))
                await self.narrate(f"{node_id}: fix claim rejected ({fault.probe_error}). Sent back with the evidence.",
                                   "high", inc)
                self.emit(ev.AgentThought(agent_id=agent, text=f"Rejected: {fault.probe_error}. Checking the log again."))
                await self.pause(1.5, 3)
                fix_tool = fault.steps[-3]
                self.emit(ev.ToolCall(agent_id=agent, tool="tail_log", args_summary="n=80", ok=True,
                                      result_summary=f"still failing: {fault.probe_error}"))
                self.emit(ev.ToolCall(agent_id=agent, tool=fix_tool[1], args_summary=fix_tool[2], ok=True,
                                      result_summary="re-applied"))
                continue

            weights = (0.55, 0.35, 0.10) if rung == "super" else (0.35, 0.45, 0.20)
            quality = self.rng.choices((5, 4, 3), weights)[0]
            await self.short_agent("verifier", "ultra", f"Root cause addressed, change is minimal. Score {quality}.",
                                   incident_id=inc, node_id=node_id)
            probes = [
                ev.Probe(name="probe_a", ok=True, ms=self.rng.uniform(200, 900), detail="200 OK, model matches"),
                ev.Probe(name="burst", ok=True, ms=self.rng.uniform(600, 2400), detail="4/4 ok"),
                ev.Probe(name="probe_b", ok=True, ms=self.rng.uniform(200, 900), detail="200 OK"),
            ]
            self.emit(ev.Verification(incident_id=inc, node_id=node_id, verdict="verified", probes=probes,
                                      quality=quality, note="All referee probes passed."))
            self.emit(ev.AgentDone(agent_id=referee))
            return quality

    async def maybe_update_skill(self, fault_class: str, fault: Fault) -> None:
        card = self.skills.get(fault_class)
        if card is not None and self.counts["verified"] % 3 != 1:
            return
        agent = self.spawn_agent("librarian", "super", prefix="lib")
        self.emit(ev.AgentThought(agent_id=agent, text=f"Folding the verified fix for {fault_class} into its card."))
        await self.pause(1.5, 3)
        if card is None:
            card = {
                "fault_class": fault_class,
                "version": 0,
                "signature": fault.symptom,
                "diagnosis": fault.steps[0][0],
                "fix_steps": [f"{tool} {args}".strip() for _, tool, args, _ in fault.steps[1:]],
                "pitfalls": [fault.pitfall],
            }
            self.skills[fault_class] = card
            summary = "Created card from a verified fix"
        else:
            summary = self.rng.choice(CARD_SUMMARIES)
        card["version"] += 1
        self.cost_other += 0.008
        self.emit(ev.SkillUpdated(fault_class=fault_class, version=card["version"], summary=summary))
        self.emit(ev.AgentDone(agent_id=agent))

    # promoter ---------------------------------------------------------------------------------

    def next_promotion(self) -> str | None:
        movable = [c for c in settings.TRAINING_FAULT_CLASSES if self.ladder[c] != "script"]
        if not movable:
            return None
        return min(movable, key=lambda c: (RUNGS.index(self.ladder[c]), settings.TRAINING_FAULT_CLASSES.index(c)))

    async def promote_cycle(self) -> None:
        async with self.promote_lock:
            self.promoter_cycles += 1
            raised = [c for c in settings.TRAINING_FAULT_CLASSES if self.ladder[c] != "super"]
            if self.promoter_cycles % 4 == 0 and raised:
                await self.demote(raised[-1])
                return
            fault_class = self.next_promotion()
            if fault_class is None:
                return
            current = self.ladder[fault_class]
            target = RUNGS[RUNGS.index(current) + 1]
            agent = self.spawn_agent("promoter", target, model="code")
            self.emit(ev.AgentThought(agent_id=agent, text=f"Trying {target} on {PROVING_TRIALS} held-out {fault_class} instances."))
            passed, trial_cost = 0, 0.0
            allowed_failures = 0 if self.promoter_cycles % 2 else 1
            for trial in range(PROVING_TRIALS):
                node_id = f"pg-{trial % 4 + 1:02d}"
                ok = trial >= allowed_failures
                self.set_status(node_id, "down", f"held-out {fault_class}")
                responder = self.spawn_agent("responder", target, prefix="pgresp", node_id=node_id)
                await self.pause(1, 2)
                self.set_status(node_id, "repairing")
                step = FAULTS[fault_class].steps[2]
                self.emit(ev.ToolCall(agent_id=responder, tool=step[1], args_summary=step[2], ok=ok,
                                      result_summary=step[3] if ok else "node still failing"))
                await self.pause(1, 2)
                trial_cost += self.sample_cost(target)
                passed += ok
                self.emit(ev.AgentDone(agent_id=responder))
                self.set_status(node_id, "healthy")
            self.cost_other += trial_cost
            mean_cost = round(trial_cost / PROVING_TRIALS, 5)
            self.emit(ev.AgentDone(agent_id=agent))
            if passed >= 4:
                self.counts["promotions"] += 1
                self.set_ladder(fault_class, current, target, "promote", passed, PROVING_TRIALS, mean_cost)
                await self.narrate(f"{fault_class} moves from {current} to {target}: {passed}/{PROVING_TRIALS} held-out passes.", "high")

    async def demote(self, fault_class: str) -> None:
        current = self.ladder[fault_class]
        previous = RUNGS[RUNGS.index(current) - 1]
        agent = self.spawn_agent("promoter", current, model="code")
        self.emit(ev.AgentThought(agent_id=agent, text=f"Live {fault_class} fixes without escalation fell below 60% on {current}."))
        await self.pause(1, 2)
        self.emit(ev.AgentDone(agent_id=agent))
        self.counts["demotions"] += 1
        self.set_ladder(fault_class, current, previous, "demote", 2, 5, round(self.sample_cost(current), 5))
        await self.narrate(f"{fault_class} moves back from {current} to {previous} after live regressions.", "high")

    def set_ladder(self, fault_class: str, old: str, new: str, direction: str, passed: int, total: int,
                   mean_cost: float) -> None:
        self.ladder[fault_class] = new
        update = ev.LadderUpdate(fault_class=fault_class, from_rung=old, to_rung=new, direction=direction,
                                 evidence=ev.LadderEvidence(passed=passed, total=total, mean_cost_usd=mean_cost))
        self.ladder_log.append({"ts": time.time(), **update.model_dump(mode="json")})
        self.emit(update)

    # operator-facing helpers ---------------------------------------------------------------------

    def briefing(self) -> str:
        away = self.away or {"counts": {k: 0 for k in self.counts}, "cost": 0.0}
        delta = {k: self.counts[k] - away["counts"][k] for k in self.counts}
        spent = self.cost_incidents + self.cost_other - away["cost"]
        return (
            f"While you were away: {delta['incidents']} incidents opened, {delta['verified']} verified, "
            f"{delta['rejected']} fix claims rejected by the referee. "
            f"{delta['promotions']} promotions and {delta['demotions']} demotions. Spend ${spent:.2f}."
        )

    async def chaos(self, command: str) -> list[dict]:
        agent = self.spawn_agent("chaos", "nano")
        text = command.lower()
        if "storm" in text:
            injected = self.storm(None)
        else:
            nodes = re.findall(r"r[1-3]-0[1-4]", text) or self.rng.sample(self.free_live_nodes() or ["r1-01"], 1)
            fault_class = next(
                (c for c in FAULTS if c in text or c.replace("_", " ") in text),
                self.rng.choice(settings.TRAINING_FAULT_CLASSES),
            )
            injected = [{"node_id": n, "fault_class": fault_class} for n in nodes if self.inject(n, fault_class)]
        summary = ", ".join(f"{i['fault_class']} on {i['node_id']}" for i in injected) or "nothing to inject"
        self.emit(ev.AgentThought(agent_id=agent, text=f"Parsed command: {summary}."))
        self.emit(ev.AgentDone(agent_id=agent))
        return injected


class ChaosRequest(BaseModel):
    command: str


class InjectRequest(BaseModel):
    node_id: str
    fault_class: str


class StormRequest(BaseModel):
    rows: list[int] | int | None = None


def create_app(speed: float = 1.0, seed: int = 7) -> FastAPI:
    sim = Sim(speed, seed)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        sim.start()
        try:
            yield
        finally:
            await sim.stop()

    app = FastAPI(title="understudy mock events", lifespan=lifespan)
    app.state.sim = sim
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        with sim.bus.subscribe() as sub:
            await sock.send_text(ev.Envelope.of(sim.snapshot()).to_json())
            await sock.send_text(ev.Envelope.of(sim.ledger()).to_json())

            async def watch_client(scope: anyio.CancelScope) -> None:
                try:
                    while True:
                        await sock.receive_text()
                except WebSocketDisconnect:
                    scope.cancel()

            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(watch_client, group.cancel_scope)
                    async for envelope in sub:
                        await sock.send_text(envelope.to_json())
            except (WebSocketDisconnect, RuntimeError):
                pass

    @app.get("/api/ledger")
    async def get_ledger() -> dict:
        return sim.ledger().model_dump(mode="json")

    @app.get("/api/ladder")
    async def get_ladder() -> dict:
        return {"ladder": dict(sim.ladder), "events": sim.ladder_log[-50:]}

    @app.get("/api/skills")
    async def get_skills() -> list[dict]:
        return list(sim.skills.values())

    @app.get("/api/history")
    async def get_history(since: float = 0.0) -> dict:
        return {"events": [e.model_dump(mode="json") for e in sim.history if e.ts > since]}

    @app.get("/api/voice/status")
    async def voice_status() -> dict:
        return {"available": False, "model": settings.MODELS["omni"].id, "reason": "mock server"}

    @app.post("/api/chaos")
    async def post_chaos(body: ChaosRequest) -> dict:
        return {"injected": await sim.chaos(body.command)}

    @app.post("/api/inject")
    async def post_inject(body: InjectRequest) -> dict:
        if body.node_id not in sim.nodes:
            raise HTTPException(404, f"unknown node {body.node_id}")
        if body.fault_class not in FAULTS:
            raise HTTPException(422, f"unknown fault class {body.fault_class}")
        if not sim.inject(body.node_id, body.fault_class):
            raise HTTPException(409, f"{body.node_id} is not a healthy live node")
        return {"injected": [{"node_id": body.node_id, "fault_class": body.fault_class}]}

    @app.post("/api/storm")
    async def post_storm(body: StormRequest | None = None) -> dict:
        rows = body.rows if body else None
        if isinstance(rows, int):
            rows = sim.rng.sample([1, 2, 3], min(max(rows, 1), 3))
        return {"injected": sim.storm(rows)}

    @app.post("/api/away")
    async def post_away() -> dict:
        sim.away = {"since": time.time(), "counts": dict(sim.counts), "cost": sim.cost_incidents + sim.cost_other}
        return {"away_since": sim.away["since"]}

    @app.post("/api/back")
    async def post_back() -> dict:
        since = sim.away["since"] if sim.away else sim.since
        text = sim.briefing()
        sim.away = None
        return {"since": since, "briefing": text, "ledger": sim.ledger().model_dump(mode="json")}

    @app.post("/api/voice")
    async def post_voice(request: Request) -> dict:
        await request.body()
        return {"transcript": "what happened while I was away", "reply": sim.briefing()}

    @app.post("/api/promote/run")
    async def post_promote() -> dict:
        if sim.promote_lock.locked():
            return {"started": False, "reason": "a promotion cycle is already running"}
        sim.run(sim.promote_cycle())
        return {"started": True}

    @app.post("/api/demo/reset")
    async def post_reset() -> dict:
        await sim.reset()
        return {"ok": True}

    return app


app = create_app(speed=float(os.environ.get("MOCK_SPEED", "1")))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=settings.MOCK_PORT, log_level="info")
