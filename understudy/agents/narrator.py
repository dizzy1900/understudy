"""Short narration lines and operator briefings."""
from __future__ import annotations

import logging
import re
import time

from understudy import llm
from understudy.context import Context
from understudy.events import Narration

log = logging.getLogger("understudy.narrator")

LINE_MAX = 160
PROMPT = (
    "Rewrite the facts as one plain sentence for an operator log, under 140 characters. "
    "Keep every node name, fault name and number exactly as given. No emoji, no exclamation marks, no extra claims."
)


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


class Narrator:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def say(
        self, facts: str, priority: str = "low", incident_id: int | None = None, polish: bool = True
    ) -> str:
        """Publishes one narration line. The facts are used as written when the model is unavailable or
        returns anything that drops a number."""
        ctx = self.ctx
        text = facts
        agent = ctx.spawn("narrator", "nano", ctx.model_id("nano"), incident_id=incident_id)
        try:
            if polish:
                text = await self._polish(facts, incident_id, agent)
        finally:
            ctx.bus.publish(Narration(text=text, priority=priority))  # type: ignore[arg-type]
            ctx.done(agent)
        return text

    async def _polish(self, facts: str, incident_id: int | None, agent: str) -> str:
        try:
            result = await self.ctx.chat(
                "narrator", "nano",
                [{"role": "system", "content": PROMPT}, {"role": "user", "content": facts}],
                thinking=False, max_tokens=90, temperature=0.2, meta={"incident_id": incident_id, "agent_id": agent},
            )
        except llm.BudgetExceeded:
            return facts
        except Exception as exc:
            log.warning("narration fell back to the plain facts: %r", exc)
            return facts
        line = " ".join(result.content.strip().strip('"').split())
        if not line or len(line) > LINE_MAX or not _numbers(facts) <= _numbers(line):
            return facts
        return line


async def briefing(db, since: float | None, now: float | None = None) -> str:
    """The spoken summary of what happened since `since`, built from the database."""
    now = time.time() if now is None else now
    since = 0.0 if since is None else since
    opened = await db.scalar("SELECT COUNT(*) FROM incidents WHERE opened_at > ?", (since,)) or 0
    verified = await db.scalar("SELECT COUNT(*) FROM incidents WHERE status = 'verified' AND closed_at > ?", (since,)) or 0
    unresolved = await db.scalar("SELECT COUNT(*) FROM incidents WHERE status = 'unresolved' AND closed_at > ?", (since,)) or 0
    rejected = await db.scalar("SELECT COUNT(*) FROM verifications WHERE verdict = 'rejected' AND ts > ?", (since,)) or 0
    promotions = await db.scalar("SELECT COUNT(*) FROM ladder_events WHERE direction = 'promote' AND ts > ?", (since,)) or 0
    demotions = await db.scalar("SELECT COUNT(*) FROM ladder_events WHERE direction = 'demote' AND ts > ?", (since,)) or 0
    spend = await db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ts > ?", (since,)) or 0.0
    text = (
        f"While you were away: {opened} incidents opened, {verified} verified, "
        f"{rejected} fix claims rejected by the referee. "
        f"{promotions} promotions and {demotions} demotions. Spend ${spend:.2f}."
    )
    if unresolved:
        text += f" {unresolved} incidents are unresolved and need a person."
    return text
