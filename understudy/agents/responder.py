"""The step-limited agent loop that works one incident."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from config import settings
from understudy.agents import protocol
from understudy.agents.protocol import REPROMPT, Turn, parse_turn
from understudy.agents.script_runner import run_script
from understudy.agents.tools import ToolBox, first_line, summarize_args
from understudy.agents.watchdog import Watchdog
from understudy.context import Context
from understudy.events import FixClaimed, ToolCall
from understudy.llm import BudgetExceeded

log = logging.getLogger("understudy.responder")

MAX_TOKENS = {"super": 4096, "lightning": 3072}


@dataclass
class Outcome:
    status: str  # "verified" or "escalate"
    reason: str = ""
    steps: int = 0
    quality: int | None = None
    claim: dict[str, str] | None = None
    infra: bool = False  # the model API failed or stalled, which says nothing about the rung's skill


def handoff_text(actions: list[dict[str, Any]], note: str) -> str:
    if not actions:
        return note
    lines = ["Earlier attempt on this incident. The node may have changed since, so check before relying on it:"]
    for row in actions[-12:]:
        result = " ".join(str(row["result"]).split())[:100]
        lines.append(f"- {row['tool']}({row['args_json'][:80]}) -> {'ok' if row['ok'] else 'error'}: {result}")
    lines.append(note)
    return "\n".join(lines)


class Responder:
    def __init__(
        self,
        ctx: Context,
        incident: dict[str, Any],
        rung: str,
        fault_class: str = "unknown",
        hint: tuple[str, float] | None = None,
        handoff: str | None = None,
    ):
        self.ctx = ctx
        self.incident = incident
        self.rung = rung
        self.fault_class = fault_class
        self.hint = hint
        self.handoff = handoff
        self._last_review: Any = None

    async def run(self) -> Outcome:
        ctx = self.ctx
        node_id = self.incident["node_id"]
        model = ctx.model_id(self.rung) if self.rung in settings.MODELS else f"{self.fault_class}.py"
        agent = ctx.spawn("responder", self.rung, model, incident_id=self.incident["id"], node_id=node_id)
        try:
            if self.rung == "script":
                return await self._run_script(agent)
            return await self._loop(agent)
        finally:
            ctx.done(agent)

    # model-driven rungs ---------------------------------------------------------------

    async def _next_turn(self, messages: list[dict[str, str]], agent: str, dog: Watchdog) -> Turn | None:
        ctx = self.ctx
        meta = {"incident_id": self.incident["id"], "agent_id": agent, "rung": self.rung}
        async with asyncio.timeout(max(20.0, dog.stall_deadline())):
            raw = ""
            for attempt in range(2):
                sent = messages
                if attempt:
                    sent = [*messages, {"role": "assistant", "content": raw[:1500] or "(no reply)"}, {"role": "user", "content": REPROMPT}]
                result = await ctx.chat(
                    "responder", self.rung, sent, thinking=True, max_tokens=MAX_TOKENS[self.rung], temperature=0.3, meta=meta
                )
                turn = parse_turn(result.content)
                if turn is not None:
                    return turn
                raw = result.content
                log.info(
                    "incident %s: unparseable reply (%d chars, %s), attempt %d: %r",
                    self.incident["id"], len(raw), result.finish_reason, attempt + 1, raw[:160],
                )
        return None

    async def _loop(self, agent: str) -> Outcome:
        ctx = self.ctx
        incident = self.incident
        skills = await ctx.db.latest_skills()
        messages = protocol.build_messages(skills, protocol.incident_context(incident, self.hint, self.handoff))
        tools = ToolBox(ctx, incident["id"], incident["node_id"], agent)
        dog = Watchdog(ctx.max_steps, ctx.stall_s)
        rejections = 0
        model_errors = 0
        while True:
            reason = dog.before_step() or dog.stalled()
            if reason:
                return Outcome("escalate", reason, dog.steps)
            try:
                turn = await self._next_turn(messages, agent, dog)
            except TimeoutError:
                return Outcome(
                    "escalate", f"no progress for {ctx.stall_s:.0f}s: the model call did not finish", dog.steps, infra=True
                )
            except BudgetExceeded:
                raise
            except Exception as exc:
                model_errors += 1
                log.warning("incident %s: model call failed (%d): %r", incident["id"], model_errors, exc)
                if model_errors >= 2:
                    return Outcome("escalate", f"model calls keep failing: {type(exc).__name__}", dog.steps, infra=True)
                continue
            if turn is None:
                return Outcome("escalate", "the model did not return a valid action after a re-prompt", dog.steps)
            model_errors = 0
            messages.append({"role": "assistant", "content": turn.canonical()})
            ctx.think(agent, turn.thought or f"Calling {turn.action}.")
            repeated = dog.is_repeat(turn.action, turn.args)
            result = await tools.call(dog.steps + 1, turn.action, turn.args)
            stuck = dog.record(turn.action, turn.args)
            if repeated:
                result.text += "\nYou already made this exact call and the answer has not changed. Try something different."
            if result.claim:
                outcome = await self._claim(agent, result.claim, dog)
                if outcome is not None:
                    return outcome
                rejections += 1
                review = self._last_review
                if rejections >= 2:
                    return Outcome("escalate", f"two fix claims rejected by the referee: {review.rejection_details()}", dog.steps)
                messages.append(protocol.rejection_message(review.rejection_details()))
                continue
            left = ctx.max_steps - dog.steps
            if 0 < left <= 3:
                result.text += f"\n{left} steps left, and claim_fixed counts as one. Claim as soon as a probe passes."
            messages.append(protocol.tool_result_message(turn.action, result.ok, result.text))
            if stuck:
                return Outcome("escalate", stuck, dog.steps)

    async def _claim(self, agent: str, claim: dict[str, str], dog: Watchdog) -> Outcome | None:
        """Returns an Outcome when the claim is verified, None when the referee rejected it."""
        ctx = self.ctx
        ctx.bus.publish(FixClaimed(incident_id=self.incident["id"], agent_id=agent, summary=claim["summary"][:240]))
        review = await ctx.verifier.review(self.incident, claim, agent, grade=self.rung != "script")
        self._last_review = review
        dog.reset_progress()
        if review.verified:
            return Outcome("verified", steps=dog.steps, quality=review.quality, claim=claim)
        return None

    # compiled-script rung ---------------------------------------------------------------

    async def _run_script(self, agent: str) -> Outcome:
        ctx = self.ctx
        incident = self.incident
        node_id = incident["node_id"]
        script = ctx.scripts_dir / f"{self.fault_class}.py"
        if not script.is_file():
            ctx.think(agent, f"No compiled script for {self.fault_class}; handing the incident up.")
            return Outcome("escalate", f"no compiled script for {self.fault_class}")
        ctx.think(agent, f"Running {script.name} on {node_id}.")
        started = time.monotonic()
        tools = ToolBox(ctx, incident["id"], node_id, agent)
        result = await run_script(tools, script)
        seconds = time.monotonic() - started
        steps = result.calls + 1
        text = result.summary if result.ok else result.error
        args = {"script": script.name}
        await ctx.db.add_action(incident["id"], agent, steps, "run_script", args, result.ok, text)
        ctx.bus.publish(
            ToolCall(
                agent_id=agent, tool="run_script", args_summary=summarize_args(args), ok=result.ok,
                result_summary=first_line(text),
            )
        )
        if not result.ok:
            return Outcome("escalate", f"script failed after {seconds:.0f}s: {first_line(result.error, 120)}", steps)
        dog = Watchdog(ctx.max_steps, ctx.stall_s)
        dog.steps = steps
        outcome = await self._claim(agent, {"summary": result.summary, "root_cause": result.root_cause}, dog)
        if outcome is not None:
            return outcome
        return Outcome("escalate", f"referee rejected the script's fix: {self._last_review.rejection_details()}", steps)
