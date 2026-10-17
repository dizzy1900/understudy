"""The briefing for an operator who has been away: figures from the database, spoken text from SUPER."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from understudy import ledger
from understudy.context import Context
from understudy.db import Database

log = logging.getLogger("understudy.briefing")

GREETING = "Welcome back."
MAX_WORDS = 70
MAX_TOKENS = 400
MODEL_TIMEOUT_S = 20.0
HOUR_S = 3600.0
COMPARISON_MIN_S = 2 * HOUR_S
LISTED_CHANGES = 1
ATTEMPTS = 2

SYSTEM = (
    "You write a spoken briefing for an on-call operator who has just come back to their desk. "
    "The user message is a JSON object of facts about the time they were away.\n"
    f"- The first words are exactly: {GREETING}\n"
    f"- Hard limit {MAX_WORDS} words in total, so aim for 50. Short sentences, one fact each. Drop details rather than go over.\n"
    "- Plain, calm spoken sentences. No lists, no markup, no emoji, and no symbols such as $ or %.\n"
    "- Use only the numbers in the JSON. Write every number as digits, exactly as it is written there "
    "(for example 1.9 cents, 107,000, 53 percent). Never add, round, convert or derive a number.\n"
    "- Do not invent causes or any detail that is not in the JSON.\n"
    "- Cover, in this order: incidents handled and how many were verified (if incidents_handled is 0, say plainly that "
    "nothing needed fixing); how many incidents are unresolved and need a person, when above 0; false fixes the referee "
    "caught; how many promotions and demotions, naming only the ones under latest_promotions and latest_demotions, for "
    "example: config syntax moved from super to lightning; the spend.\n"
    "- Only if there is room: escalations, cost per incident (compare the first and last hour only when compare_hours is "
    "true, otherwise describe no trend), tokens served, cache hit rate.\n"
    "Reply with the briefing text only."
)

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
}
MARKUP = re.compile(r"[*#_`~<>{}\[\]|$%\\]")
NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


async def compute_stats(db: Database, since: float, now: float | None = None) -> dict[str, Any]:
    """Every figure is read from the database for the window [since, now]. Incident figures cover the live zone and
    use the ledger definitions. Escalations are the sum of incidents.escalations over incidents opened in the window:
    the commander writes that column as it escalates, and it matches the count of escalation events."""
    now = time.time() if now is None else now
    live = "zone = 'live'"
    window_args = (since, now)

    async def count(sql: str) -> int:
        return int(await db.scalar(sql, window_args) or 0)

    handled = await count(f"SELECT COUNT(*) FROM incidents WHERE {live} AND opened_at BETWEEN ? AND ?")
    verified = await count(
        f"SELECT COUNT(*) FROM incidents WHERE {live} AND status = 'verified' AND opened_at BETWEEN ? AND ?"
    )
    still_open = await count(
        f"SELECT COUNT(*) FROM incidents WHERE {live} AND status = 'open' AND opened_at BETWEEN ? AND ?"
    )
    unresolved = await count(
        f"SELECT COUNT(*) FROM incidents WHERE {live} AND status = 'unresolved' AND closed_at BETWEEN ? AND ?"
    )
    escalations = await count(
        f"SELECT COALESCE(SUM(escalations), 0) FROM incidents WHERE {live} AND opened_at BETWEEN ? AND ?"
    )
    false_fixes = await count(
        "SELECT COUNT(*) FROM verifications v JOIN incidents i ON i.id = v.incident_id"
        " WHERE i.zone = 'live' AND v.verdict = 'rejected' AND v.ts BETWEEN ? AND ?"
    )
    changes = await db.fetchall(
        "SELECT direction, fault_class, from_rung, to_rung FROM ladder_events WHERE ts BETWEEN ? AND ? ORDER BY id",
        window_args,
    )

    def listed(direction: str) -> list[dict[str, str]]:
        return [
            {"fault_class": r["fault_class"], "from_rung": r["from_rung"], "to_rung": r["to_rung"]}
            for r in changes
            if r["direction"] == direction
        ]

    tokens = await count(
        "SELECT COALESCE(SUM(json_extract(data_json, '$.tokens')), 0) FROM events WHERE type = 'tokens' AND ts BETWEEN ? AND ?"
    )
    cost = float(await db.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE ts BETWEEN ? AND ?", window_args) or 0.0)
    cache = await db.fetchone(
        "SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt, COALESCE(SUM(cached_tokens), 0) AS cached"
        " FROM llm_calls WHERE ok = 1 AND ts BETWEEN ? AND ?",
        window_args,
    )
    prompt = int(cache["prompt"]) if cache else 0
    cached = int(cache["cached"]) if cache else 0

    async def cost_per_incident(start: float, end: float) -> float | None:
        row = await db.fetchone(
            f"SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM incidents"
            f" WHERE {live} AND status = 'verified' AND closed_at BETWEEN ? AND ?",
            (start, end),
        )
        return round(row["cost"] / row["n"], 4) if row and row["n"] else None

    window = max(0.0, now - since)
    return {
        "away_since": since,
        "now": now,
        "minutes_away": int(window // 60),
        "incidents_handled": handled,
        "verified": verified,
        "still_open": still_open,
        "false_fixes_caught": false_fixes,
        "escalations": escalations,
        "promotions": listed("promote"),
        "demotions": listed("demote"),
        "unresolved": unresolved,
        "tokens_served": tokens,
        "cost_total_usd": round(cost, 4),
        "cost_per_incident_usd": await cost_per_incident(since, now),
        "cost_per_incident_first_hour_usd": await cost_per_incident(since, min(now, since + HOUR_S)),
        "cost_per_incident_last_hour_usd": await cost_per_incident(max(since, now - HOUR_S), now),
        "comparison_available": window >= COMPARISON_MIN_S,
        "cache_hit_rate": round(cached / prompt, 3) if prompt else None,
    }


# Facts handed to the model ----------------------------------------------------------


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _duration(minutes: int) -> str:
    if minutes < 60:
        return _plural(minutes, "minute", "minutes")
    hours, rest = divmod(minutes, 60)
    text = _plural(hours, "hour", "hours")
    return f"{text} {_plural(rest, 'minute', 'minutes')}" if rest else text


def _money(usd: float | None) -> str | None:
    if usd is None:
        return None
    if usd >= 1:
        return f"{usd:.2f} dollars"
    cents = usd * 100
    digits = 0 if cents >= 10 else 1 if cents >= 1 else 2
    text = f"{cents:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} cents" if text != "0" else "0 cents"


def _spoken_class(name: str) -> str:
    return name.replace("_", " ")


def facts_from(stats: dict[str, Any]) -> dict[str, Any]:
    """The figures as the briefing should say them: money in dollars or cents, counts as integers, class names with
    spaces. The model may only repeat these."""

    def latest(rows: list[dict[str, str]]) -> list[str]:
        return [f"{_spoken_class(r['fault_class'])} moved from {r['from_rung']} to {r['to_rung']}" for r in rows[-LISTED_CHANGES:]]

    facts: dict[str, Any] = {
        "time_away": _duration(stats["minutes_away"]),
        "incidents_handled": stats["incidents_handled"],
        "verified": stats["verified"],
        "false_fixes_caught": stats["false_fixes_caught"],
        "escalations": stats["escalations"],
        "unresolved": stats["unresolved"],
        "promotions": len(stats["promotions"]),
        "demotions": len(stats["demotions"]),
        "tokens_served": f"{stats['tokens_served']:,}",
        "cost_total": _money(stats["cost_total_usd"]),
        "compare_hours": bool(stats["comparison_available"]),
    }
    for key in ("promotions", "demotions"):
        if stats[key]:
            facts[f"latest_{key}"] = latest(stats[key])
    if stats["still_open"]:
        facts["still_open"] = stats["still_open"]
    if stats["comparison_available"]:
        facts["cost_per_incident_first_hour"] = _money(stats["cost_per_incident_first_hour_usd"])
        facts["cost_per_incident_last_hour"] = _money(stats["cost_per_incident_last_hour_usd"])
    elif stats["cost_per_incident_usd"] is not None:
        facts["cost_per_incident"] = _money(stats["cost_per_incident_usd"])
    if stats["cache_hit_rate"] is not None:
        facts["cache_hit_rate"] = f"{round(stats['cache_hit_rate'] * 100)} percent"
    return facts


# Validation and fallback ------------------------------------------------------------


def _norm(token: str) -> float:
    return float(token.replace(",", ""))


def _numbers_in(value: Any) -> set[float]:
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, (int, float)):
        return {float(value)}
    if isinstance(value, str):
        return {_norm(t) for t in NUMBER.findall(value)}
    if isinstance(value, dict):
        return set().union(*(_numbers_in(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_numbers_in(v) for v in value)) if value else set()
    return set()


def validate(text: str, facts: dict[str, Any]) -> str | None:
    """Returns why the text cannot be used, or None when it can."""
    if not text.startswith(GREETING):
        return f"does not start with {GREETING!r}"
    words = len(text.split())
    if words > MAX_WORDS:
        return f"{words} words"
    if MARKUP.search(text) or "\n" in text:
        return "markup or symbols"
    allowed = _numbers_in(facts)
    for token in NUMBER.findall(text):
        if _norm(token) not in allowed:
            return f"number {token} is not in the facts"
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in NUMBER_WORDS and float(NUMBER_WORDS[word]) not in allowed:
            return f"spelled number {word!r} is not in the facts"
    return None


def fallback_text(facts: dict[str, Any]) -> str:
    """Deterministic briefing from the same facts. Sentences are added in order of importance while they fit."""
    handled = facts["incidents_handled"]
    if handled == 0:
        head = f"{GREETING} In {facts['time_away']} no incidents opened and nothing needed fixing."
    else:
        head = (
            f"{GREETING} In {facts['time_away']}, {_plural(handled, 'incident', 'incidents')} opened "
            f"and {facts['verified']} ended verified."
        )
    parts = [head]
    unresolved = facts["unresolved"]
    if unresolved:
        parts.append(f"{_plural(unresolved, 'incident is', 'incidents are')} unresolved and need a person.")
    if facts["false_fixes_caught"]:
        parts.append(f"The referee rejected {_plural(facts['false_fixes_caught'], 'false fix', 'false fixes')}.")
    if facts["escalations"]:
        parts.append(f"{_plural(facts['escalations'], 'escalation', 'escalations')}.")
    for key in ("promotions", "demotions"):
        if facts[key]:
            parts.append(f"{_plural(facts[key], key[:-1], key)}, latest: {'; '.join(facts['latest_' + key][-1:])}.")
    parts.append(f"Spend was {facts['cost_total']}.")
    if "cost_per_incident" in facts:
        parts.append(f"That is {facts['cost_per_incident']} per verified incident.")
    elif facts.get("cost_per_incident_first_hour") and facts.get("cost_per_incident_last_hour"):
        parts.append(
            f"Cost per incident was {facts['cost_per_incident_first_hour']} in the first hour and "
            f"{facts['cost_per_incident_last_hour']} in the last."
        )
    if handled:
        parts.append(f"{facts['tokens_served']} tokens were served.")
    if "cache_hit_rate" in facts:
        parts.append(f"Cache hit rate was {facts['cache_hit_rate']}.")
    text = parts[0]
    for part in parts[1:]:
        if len((f"{text} {part}").split()) <= MAX_WORDS:
            text = f"{text} {part}"
    return text


# Entry point ------------------------------------------------------------------------


async def spoken_text(ctx: Context, facts: dict[str, Any]) -> tuple[str, str]:
    """Returns (text, source) where source is "model" or the reason the fallback was used. A reply that fails
    validation gets one retry with the reason; the whole exchange shares one timeout."""
    agent = ctx.spawn("narrator", "super", ctx.model_id("super"))
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(facts, indent=1)}]

    async def attempts() -> tuple[str, str]:
        problem = ""
        for attempt in range(ATTEMPTS):
            result = await ctx.chat(
                "briefing", "super", messages,
                thinking=False, max_tokens=MAX_TOKENS, temperature=0.2, meta={"agent_id": agent, "purpose": "away_briefing"},
            )
            text = " ".join(result.content.strip().strip('"').split())
            problem = validate(text, facts) or ""
            if not problem:
                return text, "model"
            log.warning("briefing text rejected (%s): %s", problem, text)
            messages.extend([
                {"role": "assistant", "content": text},
                {"role": "user", "content": f"Rejected: {problem}. Write it again within the rules, shorter, using only the JSON."},
            ])
        return fallback_text(facts), problem

    try:
        return await asyncio.wait_for(attempts(), MODEL_TIMEOUT_S)
    except asyncio.TimeoutError:
        return fallback_text(facts), f"model call took longer than {MODEL_TIMEOUT_S:.0f}s"
    except Exception as exc:
        return fallback_text(facts), f"model call failed: {type(exc).__name__}"
    finally:
        ctx.done(agent)


async def build(ctx: Context, since: float | None = None, now: float | None = None) -> dict[str, Any]:
    """Returns {"text", "stats"} for the window starting at `since` (the ledger start when None)."""
    if since is None:
        since = await ledger.ledger_since(ctx.db)
    stats = await compute_stats(ctx.db, since, now)
    text, _ = await spoken_text(ctx, facts_from(stats))
    return {"text": text, "stats": stats}
