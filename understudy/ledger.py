"""Ledger numbers. Every figure is read from the database."""
from __future__ import annotations

import time
from typing import Any

from understudy.db import Database
from understudy.events import Ledger, RungStat

RUNGS = ("super", "lightning", "script")


async def ledger_since(db: Database) -> float:
    value = await db.get_setting("ledger_since")
    if value is None:
        now = time.time()
        await db.set_setting("ledger_since", repr(now))
        return now
    return float(value)


async def compute(db: Database) -> Ledger:
    since = await ledger_since(db)
    incidents_total = int(await db.scalar("SELECT COUNT(*) FROM incidents WHERE zone = 'live' AND opened_at >= ?", (since,)) or 0)
    verified = int(await db.scalar("SELECT COUNT(*) FROM incidents WHERE zone = 'live' AND status = 'verified' AND opened_at >= ?", (since,)) or 0)
    rejected = int(
        await db.scalar(
            "SELECT COUNT(*) FROM verifications v JOIN incidents i ON i.id = v.incident_id"
            " WHERE i.zone = 'live' AND v.verdict = 'rejected' AND v.ts >= ?",
            (since,),
        )
        or 0
    )
    promotions = int(await db.scalar("SELECT COUNT(*) FROM ladder_events WHERE direction = 'promote' AND ts >= ?", (since,)) or 0)
    demotions = int(await db.scalar("SELECT COUNT(*) FROM ladder_events WHERE direction = 'demote' AND ts >= ?", (since,)) or 0)
    cost_total = await db.total_cost()
    closed = await db.fetchone(
        "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM incidents"
        " WHERE zone = 'live' AND status = 'verified' AND opened_at >= ?",
        (since,),
    )
    per_incident = (closed["cost"] / closed["n"]) if closed and closed["n"] else 0.0
    tokens = await db.scalar(
        "SELECT COALESCE(SUM(json_extract(data_json, '$.tokens')), 0) FROM events WHERE type = 'tokens' AND ts >= ?",
        (since,),
    )
    prompt, cached = await db.cache_stats()
    by_rung = {name: RungStat() for name in RUNGS}
    rows = await db.fetchall(
        "SELECT rung, COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost FROM incidents"
        " WHERE zone = 'live' AND status = 'verified' AND opened_at >= ? AND rung IS NOT NULL GROUP BY rung",
        (since,),
    )
    for row in rows:
        if row["rung"] in by_rung:
            by_rung[row["rung"]] = RungStat(incidents=row["n"], cost_usd=round(row["cost"], 4))
    return Ledger(
        since=since,
        incidents_total=incidents_total,
        verified=verified,
        rejected_claims=rejected,
        promotions=promotions,
        demotions=demotions,
        cost_total_usd=round(cost_total, 4),
        cost_per_incident_usd=round(per_incident, 4),
        tokens_served=int(tokens or 0),
        cache_hit_rate=round(cached / prompt, 3) if prompt else 0.0,
        by_rung=by_rung,  # type: ignore[arg-type]
        ladder=await db.get_ladder(),  # type: ignore[arg-type]
    )


async def ladder_events(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM ladder_events ORDER BY id DESC LIMIT ?", (limit,))
    return [
        {
            "ts": r["ts"],
            "fault_class": r["fault_class"],
            "from_rung": r["from_rung"],
            "to_rung": r["to_rung"],
            "direction": r["direction"],
            "evidence": {"passed": r["passed"], "total": r["total"], "mean_cost_usd": r["mean_cost_usd"]},
        }
        for r in reversed(rows)
    ]


async def skills(db: Database) -> list[dict[str, Any]]:
    return [
        {"fault_class": r["fault_class"], "version": r["version"], "summary": r["summary"], "created_at": r["created_at"], **r["card"]}
        for r in await db.latest_skills()
    ]
