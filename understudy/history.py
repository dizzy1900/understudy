"""Time series over the incident, cost and ladder tables for the replay view."""
from __future__ import annotations

from typing import Any

from understudy.db import Database

MAX_POINTS = 500


def thin(points: list[dict[str, Any]], limit: int = MAX_POINTS) -> list[dict[str, Any]]:
    """Keep at most `limit` points spread evenly, always keeping the last one."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


async def ladder_timeline(db: Database, since: float = 0.0) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM ladder_events WHERE ts > ? ORDER BY ts, id", (since,))
    return [
        {
            "ts": r["ts"],
            "fault_class": r["fault_class"],
            "from_rung": r["from_rung"],
            "to_rung": r["to_rung"],
            "direction": r["direction"],
            "evidence": {"passed": r["passed"], "total": r["total"], "mean_cost_usd": r["mean_cost_usd"]},
        }
        for r in rows
    ]


async def incident_series(db: Database, since: float = 0.0) -> list[dict[str, Any]]:
    """Cumulative incidents opened (`total`) and verified, one point per open or verified close."""
    rows = await db.fetchall(
        "SELECT opened_at, closed_at, status FROM incidents WHERE zone = 'live' AND (opened_at > ? OR closed_at > ?)", (since, since)
    )
    steps: list[tuple[float, int, int]] = []
    for r in rows:
        if r["opened_at"] > since:
            steps.append((r["opened_at"], 1, 0))
        if r["status"] == "verified" and r["closed_at"] is not None and r["closed_at"] > since:
            steps.append((r["closed_at"], 0, 1))
    steps.sort(key=lambda s: s[0])
    total = verified = 0
    points = []
    for ts, opened, done in steps:
        total += opened
        verified += done
        points.append({"ts": ts, "total": total, "verified": verified})
    return thin(points)


async def cost_per_incident_series(db: Database, since: float = 0.0) -> list[dict[str, Any]]:
    """Running mean of incident-attributed cost over incidents closed so far."""
    rows = await db.fetchall(
        "SELECT closed_at, cost_usd FROM incidents WHERE zone = 'live' AND closed_at IS NOT NULL AND closed_at > ? ORDER BY closed_at, id",
        (since,),
    )
    cost = 0.0
    points = []
    for n, r in enumerate(rows, 1):
        cost += r["cost_usd"]
        points.append({"ts": r["closed_at"], "value": round(cost / n, 6)})
    return thin(points)


async def build_history(db: Database, since: float = 0.0) -> dict[str, Any]:
    return {
        "ladder_events": await ladder_timeline(db, since),
        "incidents": await incident_series(db, since),
        "cost_per_incident": await cost_per_incident_series(db, since),
    }
