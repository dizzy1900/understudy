"""Routes incidents to responders by ladder rung."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from typing import Any

from config import settings
from understudy.agents import triage
from understudy.agents.responder import Outcome, Responder, handoff_text
from understudy.context import Context
from understudy.events import Escalation, IncidentClosed
from understudy.llm import BudgetExceeded

log = logging.getLogger("understudy.commander")

# A failed rung hands the incident to the next more capable one.
RUNG_UP = {"script": "lightning", "lightning": "super"}
STORM_WINDOW_S = 10.0
STORM_SIZE = 3
STORM_COOLDOWN_S = 30.0
UNRESOLVED_HOLD_S = 180.0
SCAN_INTERVAL_S = 5.0
WORK_ATTEMPTS = 3


class Commander:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.agent: str | None = None
        self.active: dict[int, asyncio.Task] = {}
        self._opened: deque[float] = deque()
        self._last_storm = 0.0
        self._tasks: list[asyncio.Task] = []

    # lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        ctx = self.ctx
        self.agent = ctx.spawn("commander", "super", ctx.model_id("super"))
        ctx.think(self.agent, "On call. Watching the fleet for incidents.")
        await self.resume()
        self._tasks = [asyncio.create_task(self._listen()), asyncio.create_task(self._scan())]

    async def stop(self) -> None:
        tasks = [*self._tasks, *self.active.values()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self.active.clear()
        if self.agent:
            self.ctx.done(self.agent)

    async def resume(self) -> int:
        open_now = await self.ctx.db.open_incidents()
        if not open_now:
            return 0
        await self.ctx.say(f"Resuming {len(open_now)} open incidents.", "high", polish=False)
        for row in open_now:
            self.ctx.state.mark_repairing(row["node_id"], f"incident {row['id']} resumed")
            self.handle(row["id"], resumed=True)
        return len(open_now)

    async def _listen(self) -> None:
        with self.ctx.bus.subscribe(maxsize=2000) as sub:
            async for envelope in sub:
                if envelope.type == "incident_opened":
                    self.handle(int(envelope.data["incident_id"]))

    async def _scan(self) -> None:
        while True:
            await asyncio.sleep(SCAN_INTERVAL_S)
            try:
                for row in await self.ctx.db.open_incidents():
                    if row["id"] not in self.active:
                        self.handle(row["id"])
            except Exception:
                log.exception("incident scan failed")

    def handle(self, incident_id: int, resumed: bool = False) -> None:
        if incident_id in self.active:
            return
        task = asyncio.create_task(self.work(incident_id, resumed))
        self.active[incident_id] = task
        task.add_done_callback(lambda _t, i=incident_id: self.active.pop(i, None))

    # one incident ---------------------------------------------------------------------

    async def work(self, incident_id: int, resumed: bool = False) -> None:
        ctx = self.ctx
        failures = 0
        while failures < WORK_ATTEMPTS:
            try:
                await self._work(incident_id, resumed)
                return
            except BudgetExceeded:
                await ctx.budget_wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                log.exception("incident %s: attempt %d failed", incident_id, failures)
                await asyncio.sleep(3.0)
        row = await ctx.db.get_incident(incident_id)
        if row and row["status"] == "open":
            await self._unresolved(row, "the incident handler kept failing")

    async def _work(self, incident_id: int, resumed: bool) -> None:
        ctx = self.ctx
        db = ctx.db
        row = await db.get_incident(incident_id)
        if row is None or row["status"] != "open":
            return
        node = row["node_id"]
        ctx.state.mark_repairing(node, f"incident {incident_id}")
        if not resumed:
            await self._note_storm(row)
        fault_class = row["fault_class"]
        hint: tuple[str, float] | None = None
        if not fault_class:
            fault_class, confidence, source = await self._triage(row)
            hint = (fault_class, confidence)
            await db.update_incident(incident_id, fault_class=fault_class)
            self._thought(f"{node}: {fault_class}, confidence {confidence:.2f} ({source}).")
        ladder = await db.get_ladder()
        rung = row["rung"] or ladder.get(fault_class, "super")
        escalations = int(row["escalations"])
        actions = await db.actions_for(incident_id)
        handoff = None
        if actions:
            handoff = handoff_text(
                actions,
                "The previous responder was interrupted when the orchestrator restarted. If its actions already removed "
                "the cause and a probe passes, claim_fixed with the root cause they show.",
            )
            pending = await self._pending_claim(row, actions)
            if pending is not None:
                if pending.verified:
                    claim = json.loads(actions[-1]["args_json"])
                    await self._close_verified(row, fault_class, rung, Outcome("verified", steps=len(actions), quality=pending.quality, claim=claim))
                    return
                handoff += f"\nThe referee rejected the claim that was pending: {pending.rejection_details()}"
        while True:
            await db.update_incident(incident_id, rung=rung)
            self._thought(f"{node}: {fault_class} goes to a {rung} responder.")
            outcome = await Responder(ctx, row, rung, fault_class, hint, handoff).run()
            if outcome.status == "verified":
                await self._close_verified(row, fault_class, rung, outcome)
                return
            await self._watchdog(row, rung, outcome)
            nxt = RUNG_UP.get(rung)
            if nxt is None:
                await self._unresolved(row, outcome.reason, fault_class, rung)
                return
            ctx.bus.publish(Escalation(incident_id=incident_id, from_rung=rung, to_rung=nxt, reason=outcome.reason[:200]))  # type: ignore[arg-type]
            escalations += 1
            await db.update_incident(incident_id, escalations=escalations)
            handoff = handoff_text(
                await db.actions_for(incident_id),
                f"The {rung} responder was stopped: {outcome.reason}. You are the next rung up; take it from here.",
            )
            rung = nxt

    async def _pending_claim(self, row: dict[str, Any], actions: list[dict[str, Any]]) -> Any:
        """When the last recorded action is a claim the referee never ruled on, rule on it now."""
        last = actions[-1]
        if last["tool"] != "claim_fixed" or not last["ok"]:
            return None
        ruled = await self.ctx.db.scalar(
            "SELECT COUNT(*) FROM verifications WHERE incident_id = ? AND ts >= ?", (row["id"], last["ts"])
        )
        if ruled:
            return None
        claim = json.loads(last["args_json"])
        self._thought(f"{row['node_id']}: a fix claim was pending when the orchestrator stopped. Running the referee on it.")
        return await self.ctx.verifier.review(row, claim, last["agent_id"])

    async def _triage(self, row: dict[str, Any]) -> tuple[str, float, str]:
        ctx = self.ctx
        node = row["node_id"]
        lines = ctx.sup.tail_log(node, triage.LOG_LINES)
        if ctx.sup.status(node).state != "running":
            lines = [*lines, *ctx.sup.stderr_tail(node, 10)]
        skills = await ctx.db.latest_skills()
        result = await triage.classify(ctx, row["symptom"], lines, skills, row["id"])
        return result.fault_class, result.confidence, result.model

    def _thought(self, text: str) -> None:
        if self.agent:
            self.ctx.think(self.agent, text)

    async def _note_storm(self, row: dict[str, Any]) -> None:
        now = time.monotonic()
        self._opened.append(now)
        while self._opened and now - self._opened[0] > STORM_WINDOW_S:
            self._opened.popleft()
        if len(self._opened) < STORM_SIZE or now - self._last_storm < STORM_COOLDOWN_S:
            return
        self._last_storm = now
        count = len(self._opened)
        self._thought(f"{count} incidents in {STORM_WINDOW_S:.0f}s. Triaging each one and working them in parallel.")
        self.ctx.background(self.ctx.say(f"Storm: {count} nodes failed within {STORM_WINDOW_S:.0f} seconds. Working them in parallel.", "high"))

    async def _watchdog(self, row: dict[str, Any], rung: str, outcome: Outcome) -> None:
        ctx = self.ctx
        agent = ctx.spawn("watchdog", "script", "rules", incident_id=row["id"], node_id=row["node_id"])
        ctx.think(agent, f"Stopping the {rung} responder: {outcome.reason}")
        ctx.done(agent)

    # closing --------------------------------------------------------------------------

    async def _incident_totals(self, incident_id: int) -> tuple[float, int]:
        db = self.ctx.db
        cost = float(await db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE incident_id = ?", (incident_id,)) or 0.0)
        steps = int(await db.scalar("SELECT COUNT(*) FROM actions WHERE incident_id = ?", (incident_id,)) or 0)
        return cost, steps

    async def _close_verified(self, row: dict[str, Any], fault_class: str, rung: str, outcome: Outcome) -> None:
        ctx = self.ctx
        node = row["node_id"]
        if outcome.claim:
            fault_class = await self._diagnosed_class(row, fault_class, outcome.claim)
        cost, steps = await self._incident_totals(row["id"])
        now = time.time()
        await ctx.db.update_incident(
            row["id"], status="verified", fault_class=fault_class, rung=rung, steps=steps, cost_usd=cost,
            quality=outcome.quality, closed_at=now,
        )
        await ctx.db.clear_fault_truth(node)
        ctx.state.set_status(node, "healthy")
        if ctx.monitor is not None:
            ctx.monitor.reset(node)
        ctx.bus.publish(
            IncidentClosed(
                incident_id=row["id"], node_id=node, fault_class=fault_class, rung=rung,  # type: ignore[arg-type]
                steps=steps, cost_usd=round(cost, 6), seconds=round(now - row["opened_at"], 1),
            )
        )
        ctx.background(
            ctx.say(f"{node} is healthy again: {fault_class} fixed at the {rung} rung in {steps} steps for ${cost:.3f}.", "low", row["id"])
        )
        ctx.background(self._learn(fault_class, outcome.quality))

    async def _learn(self, fault_class: str, quality: int | None) -> None:
        """Hands a closed incident to the librarian and the promoter's demotion check."""
        ctx = self.ctx
        if ctx.librarian is not None:
            await ctx.librarian.after_verified(fault_class, quality)
        if ctx.promoter is not None:
            await ctx.promoter.after_incident(fault_class)

    async def _diagnosed_class(self, row: dict[str, Any], guess: str, claim: dict[str, str]) -> str:
        """The class the responder's own root cause points to, which is more reliable than the guess made from the
        symptom alone. Keeps the guess when the diagnosis is unclear."""
        text = f"Root cause stated by the engineer: {claim['root_cause']} Fix: {claim['summary']}"
        try:
            result = await triage.classify(self.ctx, text, [], await self.ctx.db.latest_skills(), row["id"])
        except BudgetExceeded:
            return guess
        return result.fault_class if result.fault_class != "unknown" else guess

    async def _unresolved(self, row: dict[str, Any], reason: str, fault_class: str | None = None, rung: str | None = None) -> None:
        ctx = self.ctx
        node = row["node_id"]
        cost, steps = await self._incident_totals(row["id"])
        await ctx.db.update_incident(
            row["id"], status="unresolved", steps=steps, cost_usd=cost, closed_at=time.time(),
            **({"rung": rung} if rung else {}),
        )
        ctx.state.set_status(node, "down", "unresolved")
        if ctx.monitor is not None:
            ctx.monitor.hold(node, UNRESOLVED_HOLD_S)
        name = fault_class or row.get("fault_class") or "unknown fault"
        if ctx.promoter is not None and name in settings.TRAINING_FAULT_CLASSES:
            ctx.background(ctx.promoter.after_incident(name))
        await ctx.say(f"Could not fix {node} ({name}) at any rung: {reason}. It stays down until a person looks at it.", "high", row["id"], polish=False)
