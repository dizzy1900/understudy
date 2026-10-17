"""Tests cheaper rungs on held-out proving-ground instances and moves the ladder."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from config import settings
from understudy.agents import triage
from understudy.agents.responder import Outcome, Responder
from understudy.context import Context
from understudy.events import LadderEvidence, LadderUpdate
from understudy.fleet import faults
from understudy.fleet.monitor import UNREACHABLE
from understudy.llm import BudgetExceeded

log = logging.getLogger("understudy.promoter")

CHEAPER = {"super": "lightning", "lightning": "script"}
MORE_CAPABLE = {"script": "lightning", "lightning": "super"}
ORDER = ("script", "lightning", "super")

K = 5
PASS_NEEDED = 4
CYCLE_INTERVAL_S = 360.0
FIRST_CYCLE_DELAY_S = 120.0
HELDOUT_INTERVAL_S = 1200.0
FIRST_HELDOUT_DELAY_S = 600.0
HELDOUT_PER_CLASS = 3
HELDOUT_RUNG = "lightning"
CLASSES_PER_CYCLE = 3
CLASSES_PER_MANUAL_RUN = 4
MIN_LIVE_FIXES = 2
RETRY_AFTER_S = 1800.0
TRIAL_TIMEOUT_S = 420.0
DEMOTE_WINDOW = 5
DEMOTE_BELOW = 0.6
SCRIPT_ATTEMPTS = 3
SYMPTOM_PROBES = 6
SYMPTOM_GAP_S = 1.5
INFRA_RERUNS = 2


@dataclass
class Trial:
    fault_class: str
    rung: str
    seed: int
    node_id: str
    passed: bool
    steps: int
    cost_usd: float
    reason: str = ""
    void: bool = False


def start_rung(final_rung: str, escalations: int) -> str:
    """The rung an incident began at, given where it ended and how many times it was escalated."""
    return ORDER[max(0, ORDER.index(final_rung) - escalations)] if final_rung in ORDER else final_rung


class Promoter:
    def __init__(self, ctx: Context, rng: random.Random | None = None):
        self.ctx = ctx
        self.rng = rng or random.Random()
        self.nodes = ctx.sup.node_ids("proving")
        self._pool: asyncio.Queue[str] = asyncio.Queue()
        self._cycle: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._demote_lock = asyncio.Lock()
        self._script_attempts: dict[str, int] = {}
        self.agent: str | None = None

    # lifecycle ------------------------------------------------------------------------

    async def start(self, loops: bool = True) -> None:
        ctx = self.ctx
        await ctx.db.execute("UPDATE incidents SET status = 'unresolved', closed_at = ? WHERE status = 'proving'", (time.time(),))
        await ctx.sup.reset_all("proving")
        for node in self.nodes:
            ctx.state.set_status(node, "healthy")
            self._pool.put_nowait(node)
        self.agent = ctx.spawn("promoter", "script", "rules")
        ctx.think(self.agent, f"Proving ground ready: {', '.join(self.nodes)}.")
        if loops:
            self._tasks = [asyncio.create_task(self._cycle_loop()), asyncio.create_task(self._heldout_loop())]

    async def stop(self) -> None:
        tasks = [*self._tasks, *([self._cycle] if self._cycle else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks, self._cycle = [], None
        if self.agent:
            self.ctx.done(self.agent)

    def busy(self) -> bool:
        return self._cycle is not None and not self._cycle.done()

    # scheduling -----------------------------------------------------------------------

    async def _due_in(self, key: str, interval: float, first_delay: float) -> float:
        last = await self.ctx.db.get_setting(key)
        if last is None:
            await self.ctx.db.set_setting(key, repr(time.time() - interval + first_delay))
            return first_delay
        return float(last) + interval - time.time()

    def _launch(self, work: Any) -> asyncio.Task | None:
        """Starts `work` unless a run is active. The check and the start happen without an await in between, so
        two loops cannot both begin."""
        if self.busy():
            work.close()
            return None
        self._cycle = asyncio.create_task(work)
        return self._cycle

    async def _cycle_loop(self) -> None:
        while True:
            try:
                wait = await self._due_in("promoter_last_run", CYCLE_INTERVAL_S, FIRST_CYCLE_DELAY_S)
                await asyncio.sleep(max(1.0, wait))
                if self.busy() or await self._budget_exhausted():
                    await asyncio.sleep(30.0)
                    continue
                classes, _skipped = await self.plan(CLASSES_PER_CYCLE)
                if classes:
                    task = self._launch(self._run_cycle(classes))
                    if task is None:
                        await asyncio.sleep(30.0)
                        continue
                    await self.ctx.db.set_setting("promoter_last_run", repr(time.time()))
                    await asyncio.wait({task})
                else:
                    await self.ctx.db.set_setting("promoter_last_run", repr(time.time()))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("promoter cycle failed")
                await asyncio.sleep(30.0)

    async def _heldout_loop(self) -> None:
        while True:
            try:
                wait = await self._due_in("heldout_last_run", HELDOUT_INTERVAL_S, FIRST_HELDOUT_DELAY_S)
                await asyncio.sleep(max(1.0, wait))
                if await self._budget_exhausted():
                    await asyncio.sleep(30.0)
                    continue
                task = self._launch(self.run_heldout())
                if task is None:
                    await asyncio.sleep(30.0)
                    continue
                await self.ctx.db.set_setting("heldout_last_run", repr(time.time()))
                await asyncio.wait({task})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("held-out run failed")
                await asyncio.sleep(30.0)

    async def _budget_exhausted(self) -> bool:
        gate = self.ctx.gate
        return gate is not None and await gate.exhausted()

    async def run_now(self, only: list[str] | None = None) -> dict[str, Any]:
        """Starts a promotion pass in the background and returns what it will test."""
        if self.busy():
            return {"started": False, "reason": "a proving run is already in progress"}
        if await self._budget_exhausted():
            return {"started": False, "reason": "the budget cap is reached"}
        classes, skipped = await self.plan(CLASSES_PER_MANUAL_RUN, only)
        await self.ctx.db.set_setting("promoter_last_run", repr(time.time()))
        if not classes:
            return {"started": False, "reason": "no class is ready for a cheaper rung", "skipped": skipped}
        if self._launch(self._run_cycle(classes)) is None:
            return {"started": False, "reason": "a proving run is already in progress"}
        return {
            "started": True,
            "classes": [{"fault_class": c, "from": r, "to": t} for c, r, t in classes],
            "skipped": skipped,
        }

    # planning -------------------------------------------------------------------------

    async def live_stats(self, fault_class: str, rung: str) -> tuple[int, float | None]:
        row = await self.ctx.db.fetchone(
            "SELECT COUNT(*) AS n, AVG(cost_usd) AS mean FROM incidents"
            " WHERE zone = 'live' AND status = 'verified' AND fault_class = ? AND rung = ?",
            (fault_class, rung),
        )
        n = int(row["n"]) if row else 0
        return n, (float(row["mean"]) if row and row["mean"] is not None else None)

    async def plan(
        self, limit: int, only: list[str] | None = None
    ) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
        """Returns [(class, current rung, candidate rung)] in the order they should be tried, and why the others
        were left out."""
        ctx = self.ctx
        ladder = await ctx.db.get_ladder()
        cards = {row["fault_class"]: row for row in await ctx.db.latest_skills()}
        ready: list[tuple[tuple[Any, ...], tuple[str, str, str]]] = []
        skipped: dict[str, str] = {}
        for fault_class in settings.TRAINING_FAULT_CLASSES:
            if only and fault_class not in only:
                continue
            rung = ladder.get(fault_class, "super")
            candidate = CHEAPER.get(rung)
            if candidate is None:
                skipped[fault_class] = "already at the cheapest rung"
                continue
            if candidate == "script" and not (ctx.scripts_dir / f"{fault_class}.py").is_file():
                skipped[fault_class] = "no compiled script yet"
                continue
            fixes, _mean = await self.live_stats(fault_class, rung)
            if fixes < MIN_LIVE_FIXES and not only:
                skipped[fault_class] = f"{fixes} verified live fixes at {rung}, needs {MIN_LIVE_FIXES}"
                continue
            last = await ctx.db.scalar(
                "SELECT MAX(ts) FROM heldout_reports WHERE kind = 'proving' AND fault_class = ? AND rung = ?",
                (fault_class, candidate),
            )
            card = cards.get(fault_class)
            if not only and last and time.time() - last < RETRY_AFTER_S and not (card and card["created_at"] > last):
                skipped[fault_class] = f"failed recently at {candidate} and the card has not changed since"
                continue
            ready.append(((fault_class not in cards, last or 0.0, -fixes), (fault_class, rung, candidate)))
        ready.sort(key=lambda item: item[0])
        return [item[1] for item in ready[:limit]], skipped

    # trials ---------------------------------------------------------------------------

    async def _run_cycle(self, classes: list[tuple[str, str, str]]) -> None:
        ctx = self.ctx
        agent = ctx.spawn("promoter", "script", "rules")
        try:
            names = ", ".join(f"{c} ({r} to {t})" for c, r, t in classes)
            ctx.think(agent, f"Testing {names} on {K} held-out instances each.")
            results = await asyncio.gather(*(self._evaluate(c, r, t, agent) for c, r, t in classes), return_exceptions=True)
            for (fault_class, _r, _t), result in zip(classes, results):
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    log.error("proving %s failed: %r", fault_class, result)
        finally:
            ctx.done(agent)

    async def _evaluate(self, fault_class: str, rung: str, candidate: str, agent: str) -> bool:
        trials: list[Trial] = []
        seeds = self._seeds(K)
        failures = 0

        async def one(seed: int) -> None:
            nonlocal failures
            node = await self._pool.get()
            try:
                if failures > K - PASS_NEEDED:
                    return
                trial = await self._trial(node, fault_class, candidate, seed, "proving")
                if trial.void:
                    return
                trials.append(trial)
                failures += 0 if trial.passed else 1
            finally:
                self._pool.put_nowait(node)

        for seed, result in zip(seeds, await asyncio.gather(*(one(seed) for seed in seeds), return_exceptions=True)):
            if isinstance(result, BaseException):
                log.error("trial %s seed %d failed: %r", fault_class, seed, result)
        return await self._decide(fault_class, rung, candidate, trials, agent)

    def _seeds(self, count: int) -> list[int]:
        seeds: set[int] = set()
        while len(seeds) < count:
            seeds.add(faults.random_seed("heldout", self.rng))
        return sorted(seeds)

    async def _symptom(self, node_id: str) -> str:
        ctx = self.ctx
        for _ in range(SYMPTOM_PROBES):
            probe = await ctx.referee.probe_once(node_id, "proving")
            if not probe.ok:
                text = f"probe failed: {probe.detail}"
                if any(probe.detail.startswith(p) for p in UNREACHABLE):
                    status = ctx.sup.status(node_id)
                    if status.state == "exited":
                        text += f" (node process exited, code {'unknown' if status.exit_code is None else status.exit_code})"
                return text
            await asyncio.sleep(SYMPTOM_GAP_S)
        return "probe failed: requests fail intermittently"

    async def _trial(self, node_id: str, fault_class: str, rung: str, seed: int, kind: str) -> Trial:
        """Plants one fault on a proving node, lets a responder at `rung` work it, and records the result. The
        node is reset afterwards whatever happens."""
        ctx = self.ctx
        db = ctx.db
        ctx.state.set_status(node_id, "repairing", f"proving {fault_class} at {rung}")
        incident_id: int | None = None
        outcome = Outcome("escalate", "trial did not run")
        reruns = 0
        try:
            for attempt in range(INFRA_RERUNS + 3):
                try:
                    await faults.inject(ctx.sup, node_id, fault_class, seed, db)
                    symptom = await self._symptom(node_id)
                    incident_id = await db.open_incident(node_id, symptom, "low", zone="proving", status="proving")
                    await db.update_incident(incident_id, fault_class=fault_class, rung=rung)
                    row = await db.get_incident(incident_id)
                    assert row is not None
                    hint = await self._hint(row)
                    outcome = await asyncio.wait_for(
                        Responder(ctx, row, rung, fault_class, hint).run(), TRIAL_TIMEOUT_S
                    )
                    if not outcome.infra or reruns >= INFRA_RERUNS:
                        break
                    reruns += 1
                    log.info("%s trial %s at %s seed %d on %s: model API failure (%s), running it again",
                             kind, fault_class, rung, seed, node_id, outcome.reason[:60])
                    await db.update_incident(incident_id, status="unresolved", closed_at=time.time())
                    incident_id = None
                    await self._reset(node_id)
                except BudgetExceeded:
                    if incident_id is not None:
                        await db.update_incident(incident_id, status="unresolved", closed_at=time.time())
                        incident_id = None
                    await ctx.budget_wait()
                    await self._reset(node_id)
                except TimeoutError:
                    outcome = Outcome("escalate", f"the trial did not finish within {TRIAL_TIMEOUT_S:.0f}s")
                    break
                except faults.FaultError as exc:
                    outcome = Outcome("escalate", f"the fault could not be planted: {exc}")
                    break
            return await self._record(node_id, fault_class, rung, seed, kind, incident_id, outcome)
        finally:
            await self._reset(node_id)

    async def _hint(self, row: dict[str, Any]) -> tuple[str, float] | None:
        ctx = self.ctx
        node = row["node_id"]
        lines = ctx.sup.tail_log(node, triage.LOG_LINES)
        if ctx.sup.status(node).state != "running":
            lines = [*lines, *ctx.sup.stderr_tail(node, 10)]
        result = await triage.classify(ctx, row["symptom"], lines, await ctx.db.latest_skills(), row["id"])
        return result.fault_class, result.confidence

    async def _record(
        self, node_id: str, fault_class: str, rung: str, seed: int, kind: str, incident_id: int | None, outcome: Outcome
    ) -> Trial:
        db = self.ctx.db
        passed = outcome.status == "verified"
        cost, steps = 0.0, outcome.steps
        if outcome.infra:
            if incident_id is not None:
                await db.update_incident(incident_id, status="unresolved", closed_at=time.time())
            log.warning("%s trial %s at %s seed %d on %s: voided, the model API kept failing", kind, fault_class, rung, seed, node_id)
            return Trial(fault_class, rung, seed, node_id, False, steps, 0.0, outcome.reason, void=True)
        if incident_id is not None:
            cost = float(await db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE incident_id = ?", (incident_id,)) or 0.0)
            steps = int(await db.scalar("SELECT COUNT(*) FROM actions WHERE incident_id = ?", (incident_id,)) or 0)
            await db.update_incident(
                incident_id, status="verified" if passed else "unresolved", steps=steps, cost_usd=cost,
                quality=outcome.quality, closed_at=time.time(),
            )
        detail: dict[str, Any] = {"escalated": not passed, "incident_id": incident_id}
        if passed and outcome.claim:
            detail["root_cause"] = outcome.claim.get("root_cause", "")[:200]
        if not passed:
            detail["reason"] = outcome.reason[:300]
        await db.add_heldout_report(kind, fault_class, rung, passed, seed=seed, node_id=node_id, steps=steps, cost_usd=cost, detail=detail)
        log.info("%s trial %s at %s seed %d on %s: %s in %d steps, $%.4f", kind, fault_class, rung, seed, node_id,
                 "pass" if passed else f"fail ({outcome.reason[:80]})", steps, cost)
        return Trial(fault_class, rung, seed, node_id, passed, steps, cost, outcome.reason)

    async def _reset(self, node_id: str) -> None:
        ctx = self.ctx
        try:
            await ctx.db.clear_fault_truth(node_id)
            await ctx.sup.reset(node_id)
        except Exception:
            log.exception("resetting %s failed", node_id)
        ctx.state.set_status(node_id, "healthy")

    # decisions ------------------------------------------------------------------------

    async def _decide(self, fault_class: str, rung: str, candidate: str, trials: list[Trial], agent: str) -> bool:
        ctx = self.ctx
        total = len(trials)
        passed = sum(1 for t in trials if t.passed)
        measured = sum(t.cost_usd for t in trials) / total if total else 0.0
        mean = 0.0 if candidate == "script" else measured
        _n, baseline = await self.live_stats(fault_class, rung)
        promote = total >= K and passed >= PASS_NEEDED and baseline is not None and mean < baseline
        base = f"${baseline:.4f}" if baseline is not None else "n/a"
        ctx.think(
            agent,
            f"{fault_class} at {candidate}: {passed} of {total} held-out instances fixed, mean ${mean:.4f} against "
            f"{base} live at {rung}. {'Promoting.' if promote else 'Staying at ' + rung + '.'}",
        )
        if not promote:
            if candidate == "script" and ctx.librarian is not None:
                await self._retry_script(fault_class, trials)
            return False
        await ctx.db.set_ladder(fault_class, candidate)
        await ctx.db.add_ladder_event(fault_class, rung, candidate, "promote", passed, total, mean)
        ctx.bus.publish(
            LadderUpdate(
                fault_class=fault_class, from_rung=rung, to_rung=candidate, direction="promote",  # type: ignore[arg-type]
                evidence=LadderEvidence(passed=passed, total=total, mean_cost_usd=round(mean, 6)),
            )
        )
        await ctx.say(
            f"{fault_class} moves from {rung} to {candidate}: {passed} of {total} held-out instances fixed, "
            f"mean cost ${mean:.4f} against ${baseline:.4f} per live incident at {rung}.",
            "high",
        )
        return True

    async def _retry_script(self, fault_class: str, trials: list[Trial]) -> None:
        attempts = self._script_attempts.get(fault_class, 1)
        if attempts >= SCRIPT_ATTEMPTS:
            return
        self._script_attempts[fault_class] = attempts + 1
        reasons = "; ".join(sorted({t.reason for t in trials if not t.passed and t.reason}))[:600]
        with contextlib.suppress(BudgetExceeded):
            await self.ctx.librarian.compile_script(fault_class, feedback=reasons or "the referee rejected the fix")

    async def after_incident(self, fault_class: str) -> None:
        """Demotes a class whose last DEMOTE_WINDOW live incidents at its current rung were mostly escalated or
        unresolved."""
        ctx = self.ctx
        async with self._demote_lock:
            rung = (await ctx.db.get_ladder()).get(fault_class, "super")
            if rung == "super":
                return
            since = float(
                await ctx.db.scalar("SELECT COALESCE(MAX(ts), 0) FROM ladder_events WHERE fault_class = ?", (fault_class,)) or 0.0
            )
            rows = await ctx.db.fetchall(
                "SELECT status, rung, escalations, cost_usd FROM incidents WHERE zone = 'live' AND fault_class = ?"
                " AND status IN ('verified', 'unresolved') AND closed_at > ? ORDER BY closed_at DESC",
                (fault_class, since),
            )
            here = [r for r in rows if r["rung"] and start_rung(r["rung"], int(r["escalations"])) == rung][:DEMOTE_WINDOW]
            if len(here) < DEMOTE_WINDOW:
                return
            clean = sum(1 for r in here if r["status"] == "verified" and int(r["escalations"]) == 0)
            if clean / DEMOTE_WINDOW >= DEMOTE_BELOW:
                return
            target = MORE_CAPABLE[rung]
            mean = sum(float(r["cost_usd"]) for r in here) / len(here)
            await ctx.db.set_ladder(fault_class, target)
            await ctx.db.add_ladder_event(fault_class, rung, target, "demote", clean, DEMOTE_WINDOW, mean)
            ctx.bus.publish(
                LadderUpdate(
                    fault_class=fault_class, from_rung=rung, to_rung=target, direction="demote",  # type: ignore[arg-type]
                    evidence=LadderEvidence(passed=clean, total=DEMOTE_WINDOW, mean_cost_usd=round(mean, 6)),
                )
            )
            await ctx.say(
                f"{fault_class} goes back from {rung} to {target}: only {clean} of the last {DEMOTE_WINDOW} live "
                f"incidents at {rung} were fixed without escalation.",
                "high",
            )

    # held-out classes -----------------------------------------------------------------

    async def run_heldout(self) -> list[Trial]:
        """HELDOUT_PER_CLASS instances of each held-out class at the lightning rung, failures included."""
        ctx = self.ctx
        agent = ctx.spawn("promoter", "script", "rules")
        trials: list[Trial] = []
        try:
            ctx.think(agent, f"Running {HELDOUT_PER_CLASS} instances each of {', '.join(settings.HELDOUT_FAULT_CLASSES)} at {HELDOUT_RUNG}.")

            async def one(fault_class: str, seed: int) -> None:
                node = await self._pool.get()
                try:
                    trial = await self._trial(node, fault_class, HELDOUT_RUNG, seed, "heldout_class")
                    if not trial.void:
                        trials.append(trial)
                finally:
                    self._pool.put_nowait(node)

            jobs = [one(c, s) for c in settings.HELDOUT_FAULT_CLASSES for s in self._seeds(HELDOUT_PER_CLASS)]
            await asyncio.gather(*jobs, return_exceptions=True)
            summary = ", ".join(
                f"{c} {sum(t.passed for t in trials if t.fault_class == c)} of {sum(1 for t in trials if t.fault_class == c)}"
                for c in settings.HELDOUT_FAULT_CLASSES
            )
            ctx.think(agent, f"Held-out classes at {HELDOUT_RUNG}: {summary}.")
            await ctx.say(f"Held-out check at the {HELDOUT_RUNG} rung: {summary} fixed.", "low", polish=False)
            return trials
        finally:
            ctx.done(agent)
