"""Short narration lines and operator briefings."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from understudy import llm
from understudy.context import Context
from understudy.events import Narration

log = logging.getLogger("understudy.narrator")

WORD_MAX = 18
MIN_GAP_S = 4.0
QUEUE_MAX = 3
POLISH_TIMEOUT_S = 8.0
PROMPT = (
    "You write one line for an operations log. Rewrite the facts as one calm, factual sentence of at most "
    f"{WORD_MAX} words. Keep every node name, fault class name and number exactly as given, with the same digits. "
    "Plain words, no emoji, no exclamation marks, no new claims, no advice. Reply with the sentence only."
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_NODE = re.compile(r"\b(?:r\d-\d\d|pg-\d\d)\b")
_FAULT = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")


def word_count(text: str) -> int:
    return len(text.split())


def _keep_tokens(text: str) -> set[str]:
    return set(_NUMBER.findall(text)) | set(_NODE.findall(text)) | set(_FAULT.findall(text))


def accept(line: str, facts: str, max_words: int | None = WORD_MAX) -> bool:
    """True when a rewritten line is one short ASCII line that still carries every number, node id and fault
    class name of the facts."""
    if not line or "\n" in line or "!" in line or not line.isascii():
        return False
    if max_words is not None and word_count(line) > max_words:
        return False
    return _keep_tokens(facts) <= _keep_tokens(line)


# Fact strings, one per narrated event. Each stays within WORD_MAX words so the plain facts are a valid line.

PROBE_WORDS = {"probe_a": "the first health probe", "probe_b": "the second health probe", "burst": "the burst"}


def probe_words(name: str) -> str:
    return PROBE_WORDS.get("burst" if name.startswith("burst") else name, "the health check")


def rejection_line(node_id: str, probe_name: str) -> str:
    return f"{node_id} reported fixed. {probe_words(probe_name).capitalize()} failed. Reopening."


_REASONS = (
    (re.compile(r"step budget"), "step budget used up"),
    (re.compile(r"no progress for (\d+)s"), lambda m: f"no progress for {m.group(1)}s"),
    (re.compile(r"(\w+) repeated (\d+) times"), lambda m: f"{m.group(1)} repeated {m.group(2)} times"),
    (re.compile(r"two fix claims rejected"), "two fix claims rejected"),
    (re.compile(r"referee rejected the script"), "the script's fix was rejected"),
    (re.compile(r"no compiled script for (\w+)"), lambda m: f"no compiled script for {m.group(1)}"),
    (re.compile(r"script failed"), "the script failed"),
    (re.compile(r"model calls keep failing"), "model calls keep failing"),
    (re.compile(r"did not return a valid action"), "no valid action from the model"),
)


def short_reason(reason: str, max_words: int = 6) -> str:
    for pattern, short in _REASONS:
        match = pattern.search(reason)
        if match:
            return short(match) if callable(short) else short
    head = re.split(r"[:;.]", reason.strip(), maxsplit=1)[0].split()
    return " ".join(head[:max_words]) or "no reason recorded"


def escalation_line(node_id: str, from_rung: str, to_rung: str, reason: str) -> str:
    return f"{node_id} escalated from {from_rung} to {to_rung}: {short_reason(reason, 8)}."


def storm_line(count: int, window_s: float) -> str:
    return f"Storm: {count} nodes failed within {window_s:.0f} seconds. Working them in parallel."


def healthy_line(node_id: str, fault_class: str, rung: str, steps: int, cost_usd: float) -> str:
    return f"{node_id} is healthy again: {fault_class} fixed at the {rung} rung in {steps} steps for ${cost_usd:.3f}."


def unresolved_line(node_id: str, fault_class: str, reason: str) -> str:
    return f"{node_id} ({fault_class}) is unresolved: {short_reason(reason)}. It needs a person."


def resume_line(count: int) -> str:
    return f"Resuming {count} open incidents."


def promotion_line(fault_class: str, from_rung: str, to_rung: str, passed: int, total: int, mean: float, baseline: float) -> str:
    return (
        f"{fault_class} promoted from {from_rung} to {to_rung}: {passed} of {total} held-out runs fixed, "
        f"mean cost ${mean:.4f} against ${baseline:.4f}."
    )


def demotion_line(fault_class: str, from_rung: str, to_rung: str, clean: int, window: int) -> str:
    return (
        f"{fault_class} demoted from {from_rung} to {to_rung}: only {clean} of the last {window} live incidents "
        "were fixed without escalation."
    )


def heldout_line(rung: str, summary: str) -> str:
    return f"Held-out check at the {rung} rung: {summary} fixed."


@dataclass
class _Line:
    facts: str
    priority: str
    incident_id: int | None
    polish: bool


class Narrator:
    """Publishes narration at most once per MIN_GAP_S. say() only queues; a short-lived task publishes. Low
    priority lines are dropped when the narrator is busy, high priority lines wait their turn."""

    def __init__(
        self,
        ctx: Context,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        gap_s: float = MIN_GAP_S,
    ):
        self.ctx = ctx
        self._clock = clock
        self._sleep = sleep
        self.gap_s = gap_s
        self._queue: deque[_Line] = deque()
        self._last: float | None = None
        self._busy = False
        self._task: asyncio.Task | None = None

    async def say(
        self, facts: str, priority: str = "low", incident_id: int | None = None, polish: bool = True, force: bool = False
    ) -> str:
        """Returns the facts at once. With force the line is published now, ignoring the rate limit and the word
        cap, and the published text is returned."""
        try:
            line = _Line(facts, priority, incident_id, polish)
            if force:
                return await self._publish(line, max_words=None)
            self._enqueue(line)
        except Exception:
            log.exception("narration failed")
        return facts

    async def close(self) -> None:
        task, self._task = self._task, None
        self._queue.clear()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def drain(self) -> None:
        """Waits until everything queued has been published."""
        while self._task is not None and not self._task.done():
            await asyncio.wait({self._task})

    def _is_busy(self) -> bool:
        if self._queue or self._busy:
            return True
        return self._last is not None and self._clock() - self._last < self.gap_s

    def _enqueue(self, line: _Line) -> None:
        if line.priority != "high":
            if self._is_busy():
                return
        elif len(self._queue) >= QUEUE_MAX:
            victim = next((q for q in self._queue if q.priority != "high"), self._queue[0])
            self._queue.remove(victim)
        self._queue.append(line)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while self._queue:
            if self._last is not None:
                wait = self._last + self.gap_s - self._clock()
                if wait > 0:
                    await self._sleep(wait)
                    continue
            line = self._queue.popleft()
            try:
                await self._publish(line)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("narration failed")

    async def _publish(self, line: _Line, max_words: int | None = WORD_MAX) -> str:
        ctx = self.ctx
        self._busy = True
        text = line.facts
        agent = None
        try:
            if line.polish:
                agent = ctx.spawn("narrator", "nano", ctx.model_id("nano"), incident_id=line.incident_id)
                text = await self._polish(line.facts, line.incident_id, agent, max_words)
            ctx.bus.publish(Narration(text=text, priority=line.priority))  # type: ignore[arg-type]
        finally:
            self._busy = False
            self._last = self._clock()
            if agent is not None:
                ctx.done(agent)
        return text

    async def _polish(self, facts: str, incident_id: int | None, agent: str, max_words: int | None) -> str:
        try:
            result = await asyncio.wait_for(
                self.ctx.chat(
                    "narrator", "nano",
                    [{"role": "system", "content": PROMPT}, {"role": "user", "content": facts}],
                    thinking=False, max_tokens=60, temperature=0.2, meta={"incident_id": incident_id, "agent_id": agent},
                ),
                POLISH_TIMEOUT_S,
            )
        except llm.BudgetExceeded:
            return facts
        except Exception as exc:
            # A slow model must not hold up the lines queued behind this one.
            log.warning("narration fell back to the plain facts: %r", exc)
            return facts
        line = result.content.strip().strip('"').strip()
        return line if accept(line, facts, max_words) else facts
