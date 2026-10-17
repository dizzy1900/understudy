"""Handles the agents share: database, event bus, fleet objects and the chat function."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from config import settings
from understudy import llm
from understudy.db import Database
from understudy.events import AgentDone, AgentSpawned, AgentThought, EventBus
from understudy.fleet.referee import Referee
from understudy.fleet.state import FleetState
from understudy.fleet.supervisor import Supervisor

log = logging.getLogger("understudy.agents")

ChatFn = Callable[..., Awaitable[llm.LLMResult]]
SayFn = Callable[..., Awaitable[Any]]


class BudgetGate:
    """Holds LLM work while spend is at or over the cap, and says so once."""

    def __init__(
        self,
        spent: Callable[[], Awaitable[float]],
        limit: float | None,
        say: SayFn | None = None,
        poll_s: float = 10.0,
    ):
        self._spent = spent
        self.limit = limit
        self.say = say
        self.poll_s = poll_s
        self.paused = False

    async def exhausted(self) -> bool:
        return self.limit is not None and await self._spent() >= self.limit

    async def wait(self) -> None:
        if self.limit is None:
            return
        spent = await self._spent()
        if spent < self.limit:
            return
        if not self.paused:
            self.paused = True
            log.warning("budget cap reached: $%.2f of $%.2f", spent, self.limit)
            if self.say is not None:
                await self.say(f"Budget cap reached: ${spent:.2f} of ${self.limit:.2f} spent. LLM work is paused.", "high")
        while await self._spent() >= self.limit:
            await asyncio.sleep(self.poll_s)
        if self.paused:
            self.paused = False
            if self.say is not None:
                await self.say("Budget is available again. LLM work resumes.", "high")


@dataclass
class Context:
    db: Database
    bus: EventBus
    sup: Supervisor
    state: FleetState
    referee: Referee
    chat: ChatFn = llm.chat
    monitor: Any = None
    verifier: Any = None
    narrator: Any = None
    resolve: Callable[[str], tuple[str, str]] = lambda key: (key, settings.MODELS[key].id)
    gate: BudgetGate | None = None
    max_steps: int = 14
    stall_s: float = 90.0
    scripts_dir: Any = settings.DATA_DIR / "scripts"
    _ids: Any = field(default_factory=itertools.count, repr=False)
    _tag: str = field(default_factory=lambda: format(int(time.time()) % 46656, "03x"), repr=False)

    def model_id(self, key: str) -> str:
        return self.resolve(key)[1]

    def new_agent_id(self, prefix: str) -> str:
        return f"{prefix}-{self._tag}{next(self._ids)}"

    def spawn(
        self, role: str, rung: str, model: str, *, incident_id: int | None = None, node_id: str | None = None,
        prefix: str | None = None,
    ) -> str:
        agent_id = self.new_agent_id(prefix or role)
        self.bus.publish(
            AgentSpawned(agent_id=agent_id, role=role, model=model, rung=rung, incident_id=incident_id, node_id=node_id)
        )
        return agent_id

    def think(self, agent_id: str, text: str) -> None:
        self.bus.publish(AgentThought(agent_id=agent_id, text=" ".join(text.split())))

    def done(self, agent_id: str) -> None:
        self.bus.publish(AgentDone(agent_id=agent_id))

    async def budget_wait(self) -> None:
        if self.gate is not None:
            await self.gate.wait()
        else:
            await asyncio.sleep(1.0)

    async def say(self, facts: str, priority: str = "low", incident_id: int | None = None, polish: bool = True) -> str:
        if self.narrator is None:
            return facts
        return await self.narrator.say(facts, priority, incident_id=incident_id, polish=polish)

    def background(self, coro: Awaitable[Any]) -> "asyncio.Task":
        task = asyncio.ensure_future(coro)
        task.add_done_callback(_log_failure)
        return task


def _log_failure(task: "asyncio.Future") -> None:
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
        if exc is not None:
            log.error("background task failed: %r", exc)
