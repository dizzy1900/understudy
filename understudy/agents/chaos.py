"""Turns natural-language commands into fault injections, and drives the autopilot fault schedule."""
from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any

from config import settings
from understudy import llm
from understudy.context import Context
from understudy.fleet import faults

log = logging.getLogger("understudy.chaos")

MAX_DOWN = 6
FAULT_INTERVAL_S = (20.0, 40.0)
STORM_INTERVAL_S = 300.0
FIRST_STORM_S = 150.0
NODE_RE = re.compile(r"\br([1-3])-0([1-4])\b")

PARSER_PROMPT = (
    "You turn an operator's chaos command into JSON for a test harness. Fault classes: {classes}. "
    "Nodes are named r<row>-0<col> with row 1 to 3 and col 1 to 4. Reply with one JSON object only: "
    '{{"storm": true or false, "rows": [row numbers] or null, '
    '"targets": [{{"node_id": "r1-02" or null, "fault_class": "<class>" or null, "count": <int>}}]}}. '
    "Set storm to true when the operator asks for a storm or many mixed faults at once. "
    "Use null for anything the operator did not specify."
)


class ChaosError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ChaosOps:
    def __init__(self, ctx: Context, rng: random.Random | None = None):
        self.ctx = ctx
        self.rng = rng or random.Random()
        self._busy: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    # selection ------------------------------------------------------------------------

    async def _faulty_nodes(self) -> set[str]:
        return {row["node_id"] for row in await self.ctx.db.active_faults()}

    async def free_live_nodes(self, rows: list[int] | None = None) -> list[str]:
        faulty = await self._faulty_nodes()
        out = []
        for node_id, view in self.ctx.state.nodes.items():
            if view.spec.zone != "live" or view.status != "healthy":
                continue
            if node_id in faulty or node_id in self._busy or (rows and view.spec.row not in rows):
                continue
            if self.ctx.sup.status(node_id).state != "running":
                continue
            out.append(node_id)
        return out

    async def down_count(self) -> int:
        state = self.ctx.state
        unhealthy = {n for n, v in state.nodes.items() if v.spec.zone == "live" and v.status != "healthy"}
        faulty = {n for n in await self._faulty_nodes() if state.nodes[n].spec.zone == "live"}
        return len(unhealthy | faulty | self._busy)

    # injection ------------------------------------------------------------------------

    async def inject(self, node_id: str, fault_class: str, seed: int | None = None) -> dict[str, Any]:
        ctx = self.ctx
        if node_id not in ctx.sup.specs:
            raise ChaosError(404, f"unknown node {node_id}")
        try:
            fault_class = faults.resolve_class(fault_class)
        except faults.FaultError as exc:
            raise ChaosError(422, str(exc)) from None
        view = ctx.state.nodes[node_id]
        if view.spec.zone != "live" or view.status != "healthy" or node_id in self._busy or await ctx.db.active_faults(node_id):
            raise ChaosError(409, f"{node_id} is not a healthy live node")
        self._busy.add(node_id)
        try:
            kind = "heldout" if fault_class in settings.HELDOUT_FAULT_CLASSES else "train"
            fault = await faults.inject(ctx.sup, node_id, fault_class, seed or faults.random_seed(kind, self.rng), ctx.db)
        except faults.FaultError as exc:
            raise ChaosError(500, str(exc)) from None
        finally:
            self._busy.discard(node_id)
        log.info("injected %s on %s (seed %s)", fault_class, node_id, fault.seed)
        return {"node_id": node_id, "fault_class": fault_class}

    async def random_fault(self) -> dict[str, Any] | None:
        free = await self.free_live_nodes()
        if not free:
            return None
        node_id = self.rng.choice(free)
        fault_class = self.rng.choice(settings.TRAINING_FAULT_CLASSES)
        try:
            return await self.inject(node_id, fault_class)
        except ChaosError as exc:
            log.warning("random fault skipped: %s", exc.message)
            return None

    async def storm(self, rows: list[int] | int | None = None, count: int | None = None) -> list[dict[str, Any]]:
        ctx = self.ctx
        room = MAX_DOWN - await self.down_count()
        seed = faults.random_seed("train", self.rng)
        nodes = await faults.pick_storm_nodes(ctx.sup, seed, rows, count, ctx.db)
        nodes = [n for n in nodes if ctx.state.status(n) == "healthy" and n not in self._busy][: max(0, room)]
        if not nodes:
            return []
        self._busy.update(nodes)
        try:
            planted = await faults.storm(ctx.sup, nodes, seed, ctx.db)
        except faults.FaultError as exc:
            raise ChaosError(500, str(exc)) from None
        finally:
            self._busy.difference_update(nodes)
        log.info("storm on %s", ", ".join(nodes))
        return [{"node_id": f.node_id, "fault_class": f.fault_class} for f in planted]

    # natural language -----------------------------------------------------------------

    async def command(self, text: str) -> list[dict[str, Any]]:
        ctx = self.ctx
        agent = ctx.spawn("chaos", "nano", ctx.model_id("nano"))
        try:
            plan = await self._parse(text, agent)
            injected = await self._execute(plan)
            summary = ", ".join(f"{i['fault_class']} on {i['node_id']}" for i in injected) or "nothing to inject"
            ctx.think(agent, f"Parsed command: {summary}.")
            return injected
        finally:
            ctx.done(agent)

    async def _parse(self, text: str, agent: str) -> dict[str, Any]:
        classes = ", ".join(faults.ALL_CLASSES)
        try:
            result = await self.ctx.chat(
                "chaos", "nano",
                [{"role": "system", "content": PARSER_PROMPT.format(classes=classes)}, {"role": "user", "content": text}],
                thinking=False, max_tokens=200, temperature=0.0, meta={"agent_id": agent},
            )
            plan = llm.parse_action(result.content)
            if isinstance(plan, dict):
                return plan
        except llm.BudgetExceeded:
            pass
        except Exception as exc:
            log.warning("chaos parser fell back to keywords: %r", exc)
        return self._keywords(text)

    @staticmethod
    def _keywords(text: str) -> dict[str, Any]:
        lowered = text.lower()
        nodes = [f"r{r}-0{c}" for r, c in NODE_RE.findall(lowered)]
        cls = next((c for c in faults.ALL_CLASSES if c in lowered or c.replace("_", " ") in lowered), None)
        if "storm" in lowered:
            return {"storm": True, "rows": None, "targets": []}
        targets = [{"node_id": n, "fault_class": cls, "count": 1} for n in nodes] or [{"node_id": None, "fault_class": cls, "count": 1}]
        return {"storm": False, "rows": None, "targets": targets}

    async def _execute(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        rows = plan.get("rows")
        rows = [int(r) for r in rows if str(r).isdigit() and 1 <= int(r) <= 3] if isinstance(rows, list) else None
        if plan.get("storm"):
            return await self.storm(rows or None)
        injected: list[dict[str, Any]] = []
        for target in plan.get("targets") or []:
            if not isinstance(target, dict):
                continue
            cls = target.get("fault_class")
            if cls not in faults.ALL_CLASSES:
                cls = self.rng.choice(settings.TRAINING_FAULT_CLASSES)
            node_id = target.get("node_id")
            try:
                count = max(1, min(int(target.get("count") or 1), MAX_DOWN))
            except (TypeError, ValueError):
                count = 1
            if node_id in self.ctx.sup.specs:
                chosen = [node_id]
            else:
                free = await self.free_live_nodes(rows)
                chosen = self.rng.sample(free, min(count, len(free)))
            for node in chosen:
                try:
                    injected.append(await self.inject(node, cls))
                except ChaosError as exc:
                    log.warning("command skipped %s: %s", node, exc.message)
        return injected

    # autopilot ------------------------------------------------------------------------

    async def _fault_loop(self) -> None:
        while True:
            await asyncio.sleep(self.rng.uniform(*FAULT_INTERVAL_S))
            if await self._held_back():
                continue
            try:
                await self.random_fault()
            except Exception:
                log.exception("scheduled fault failed")

    async def _storm_loop(self) -> None:
        delay = FIRST_STORM_S
        while True:
            await asyncio.sleep(delay)
            delay = STORM_INTERVAL_S * self.rng.uniform(0.9, 1.1)
            if await self._held_back():
                continue
            try:
                await self.storm(rows=1)
            except Exception:
                log.exception("scheduled storm failed")

    async def _held_back(self) -> bool:
        gate = self.ctx.gate
        if gate is not None and gate.paused:
            return True
        return await self.down_count() >= MAX_DOWN

    def start_autopilot(self) -> None:
        self._tasks = [asyncio.create_task(self._fault_loop()), asyncio.create_task(self._storm_loop())]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
