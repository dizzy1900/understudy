"""Runs the referee on a fix claim and grades verified trajectories."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from understudy import llm
from understudy.agents import narrator
from understudy.context import Context
from understudy.events import Verification
from understudy.fleet.referee import Verdict

log = logging.getLogger("understudy.verifier")

GRADE_TIMEOUT_S = 60.0
# A responder gets its first rejected claim back; the second one ends its run and the escalation line covers it.
REOPEN_LIMIT = 1

GRADER_PROMPT = (
    "You review a fix an on-call engineer made to an inference node. The node already passed an independent "
    "health check; you judge the quality of the work. Grade 1 to 5:\n"
    "5 = the root cause was found and removed with a minimal, safe change.\n"
    "4 = correct root cause, change slightly broader than needed.\n"
    "3 = it works but the cause is only partly explained, or the path was roundabout.\n"
    "2 = it treated a symptom and the cause is probably still there.\n"
    "1 = unsafe or unrelated change.\n"
    'Reply with one JSON object only: {"quality": <1-5>, "reason": "<one short line>"}'
)


@dataclass
class Review:
    verdict: Verdict
    quality: int | None = None
    reason: str = ""

    @property
    def verified(self) -> bool:
        return self.verdict.verified

    def rejection_details(self) -> str:
        failed = [p for p in self.verdict.probes if not p.ok]
        if not failed:
            return self.verdict.note
        lines = [f"{p.name}: {p.detail}" for p in failed[:3]]
        passed = len(self.verdict.probes) - len(failed)
        return f"{self.verdict.note}. " + "; ".join(lines) + f" ({passed} of {len(self.verdict.probes)} probes passed)"


def render_trajectory(actions: list[dict[str, Any]], limit: int = 20) -> str:
    lines = []
    for row in actions[-limit:]:
        result = " ".join(str(row["result"]).split())[:160]
        lines.append(f"{row['step']}. {row['tool']}({row['args_json'][:120]}) -> {'ok' if row['ok'] else 'error'}: {result}")
    return "\n".join(lines) or "(no actions)"


class Verifier:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._rejections: dict[int, int] = {}

    def forget(self, incident_id: int) -> None:
        """Starts the rejection count over, for a new responder on the same incident."""
        self._rejections.pop(incident_id, None)

    async def review(
        self, incident: dict[str, Any], claim: dict[str, str], responder_id: str, grade: bool = True
    ) -> Review:
        ctx = self.ctx
        node_id = incident["node_id"]
        agent = ctx.spawn(
            "verifier", "ultra", ctx.model_id("ultra"), incident_id=incident["id"], node_id=node_id
        )
        try:
            ctx.think(agent, f"Running the referee on {node_id}: probe, 8s wait, burst of 4, probe.")
            verdict = await ctx.referee.verify(node_id)
            review = Review(verdict)
            if verdict.verified and grade:
                review.quality, review.reason = await self._grade(incident, claim, agent)
            note = verdict.note
            if review.quality is not None:
                note = f"{note}. Quality {review.quality}/5: {review.reason}"
            ctx.think(agent, f"Referee: {verdict.verdict}. {verdict.note}")
            await ctx.db.add_verification(
                incident["id"], node_id, verdict.verdict, [p.to_dict() for p in verdict.probes], review.quality, note
            )
            ctx.bus.publish(
                Verification(
                    incident_id=incident["id"], node_id=node_id, verdict=verdict.verdict,  # type: ignore[arg-type]
                    probes=[p.to_event() for p in verdict.probes], quality=review.quality, note=note,
                )
            )
            await self._narrate(incident, review, grade)
            return review
        finally:
            ctx.done(agent)

    async def _narrate(self, incident: dict[str, Any], review: Review, grade: bool) -> None:
        incident_id = incident["id"]
        if review.verified:
            self._rejections.pop(incident_id, None)
            return
        count = self._rejections[incident_id] = self._rejections.get(incident_id, 0) + 1
        # Proving-ground trials are not live incidents, and a compiled script is escalated without a retry.
        if incident.get("zone") == "proving" or not grade or count > REOPEN_LIMIT:
            return
        failed = next((p for p in review.verdict.probes if not p.ok), None)
        line = narrator.rejection_line(incident["node_id"], failed.name if failed else "")
        await self.ctx.say(line, "high", incident_id)

    async def _grade(self, incident: dict[str, Any], claim: dict[str, str], agent: str) -> tuple[int | None, str]:
        ctx = self.ctx
        actions = await ctx.db.actions_for(incident["id"])
        user = (
            f"Incident symptom: {incident['symptom']}\n\nActions taken:\n{render_trajectory(actions)}\n\n"
            f"Claim summary: {claim['summary']}\nClaimed root cause: {claim['root_cause']}"
        )
        messages = [{"role": "system", "content": GRADER_PROMPT}, {"role": "user", "content": user}]
        try:
            result = await asyncio.wait_for(
                ctx.chat(
                    "verifier", "ultra", messages, thinking=False, max_tokens=300, temperature=0.0,
                    meta={"incident_id": incident["id"], "agent_id": agent},
                ),
                GRADE_TIMEOUT_S,
            )
        except llm.BudgetExceeded:
            return None, "not graded: budget cap reached"
        except Exception as exc:
            log.warning("grading failed: %r", exc)
            return None, "not graded: reviewer unavailable"
        parsed = llm.parse_action(result.content) or {}
        try:
            quality = max(1, min(5, int(round(float(parsed["quality"])))))
        except (KeyError, TypeError, ValueError):
            return None, "not graded: unreadable reviewer reply"
        reason = " ".join(str(parsed.get("reason", "")).split())[:120]
        ctx.think(agent, f"Quality {quality}/5. {reason}")
        return quality, reason
