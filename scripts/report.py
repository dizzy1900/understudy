"""Prints run results from the database as markdown and writes them as JSON."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings
from understudy import history
from understudy.db import Database

HALF_HOUR = 1800.0
RUNGS = ("super", "lightning", "script")


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _window_stats(rows: list[dict]) -> dict[str, Any]:
    verified = sum(1 for r in rows if r["status"] == "verified")
    return {
        "incidents": len(rows),
        "verified": verified,
        "success_rate": round(verified / len(rows), 4) if rows else None,
        "mean_cost": _mean([r["cost_usd"] for r in rows]),
    }


def _windows(start: float | None, end: float | None) -> dict[str, Any]:
    if start is None or end is None:
        return {"start": start, "end": end, "mode": "empty", "first": None, "last": None}
    if end - start >= 2 * HALF_HOUR:
        return {"start": start, "end": end, "mode": "30min",
                "first": (start, start + HALF_HOUR), "last": (end - HALF_HOUR, end)}
    mid = (start + end) / 2
    return {"start": start, "end": end, "mode": "halves", "first": (start, mid), "last": (mid, end)}


def _in(window: tuple[float, float] | None, ts: float, last: bool) -> bool:
    if window is None:
        return False
    lo, hi = window
    return lo <= ts <= hi if last else lo <= ts < hi


def _rung_tally(rows: list[dict]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rung in sorted({r["rung"] for r in rows}, key=lambda x: RUNGS.index(x) if x in RUNGS else 99):
        sel = [r for r in rows if r["rung"] == rung]
        out[rung] = {"passed": sum(r["passed"] for r in sel), "total": len(sel),
                     "mean_cost_usd": _mean([r["cost_usd"] for r in sel]),
                     "mean_steps": _mean([r["steps"] for r in sel])}
    return out


def _detail(row: dict) -> dict[str, Any]:
    try:
        value = json.loads(row["detail_json"]) if row["detail_json"] else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


async def collect(db: Database) -> dict[str, Any]:
    incidents = await db.fetchall("SELECT * FROM incidents WHERE zone = 'live' ORDER BY id")
    closed = [r for r in incidents if r["closed_at"] is not None]
    start = min((r["opened_at"] for r in incidents), default=None)
    end = max((r["closed_at"] for r in closed), default=None)
    win = _windows(start, end)

    per_class: dict[str, Any] = {}
    for name in sorted({r["fault_class"] or "unknown" for r in closed}):
        rows = [r for r in closed if (r["fault_class"] or "unknown") == name]
        per_class[name] = {
            "overall": _window_stats(rows),
            "first": _window_stats([r for r in rows if _in(win["first"], r["closed_at"], False)]),
            "last": _window_stats([r for r in rows if _in(win["last"], r["closed_at"], True)]),
        }
    overall = {
        "overall": _window_stats(closed),
        "first": _window_stats([r for r in closed if _in(win["first"], r["closed_at"], False)]),
        "last": _window_stats([r for r in closed if _in(win["last"], r["closed_at"], True)]),
    }

    llm_total = await db.total_cost()
    attributed = sum(r["cost_usd"] for r in closed)
    llm_by_role = await db.fetchall(
        "SELECT role, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost FROM llm_calls GROUP BY role ORDER BY cost DESC"
    )
    costs = {
        "closed_incidents": len(closed),
        "llm_cost_usd": round(llm_total, 6),
        "llm_cost_per_incident": round(llm_total / len(closed), 6) if closed else None,
        "attributed_cost_usd": round(attributed, 6),
        "attributed_cost_per_incident": round(attributed / len(closed), 6) if closed else None,
        "by_role": [{"role": r["role"], "calls": r["calls"], "cost_usd": round(r["cost"], 6)} for r in llm_by_role],
    }

    prompt, cached = await db.cache_stats()
    ladder_rows = await history.ladder_timeline(db)
    timeline: dict[str, list[dict]] = {}
    for ev in ladder_rows:
        timeline.setdefault(ev["fault_class"], []).append(
            {"ts": ev["ts"], "from_rung": ev["from_rung"], "to_rung": ev["to_rung"], "direction": ev["direction"]})

    reports = await db.fetchall("SELECT * FROM heldout_reports ORDER BY ts, id")
    proving_rows = [r for r in reports if r["kind"] == "proving"]
    heldout_rows = [r for r in reports if r["kind"] == "heldout_class"]
    proving = {name: _rung_tally([r for r in proving_rows if r["fault_class"] == name])
               for name in sorted({r["fault_class"] for r in proving_rows})}
    heldout = {}
    for name in sorted({r["fault_class"] for r in heldout_rows}):
        rows = [r for r in heldout_rows if r["fault_class"] == name]
        heldout[name] = {
            "by_rung": _rung_tally(rows),
            "runs": [{"ts": r["ts"], "rung": r["rung"], "seed": r["seed"], "passed": bool(r["passed"]),
                      "steps": r["steps"], "cost_usd": r["cost_usd"], "reason": _detail(r).get("reason")}
                     for r in rows],
        }

    def pass_total(rows: list[dict]) -> dict[str, Any]:
        n = len(rows)
        p = sum(r["passed"] for r in rows)
        return {"passed": p, "total": n, "rate": round(p / n, 4) if n else None}

    # ledger_since() would create the setting; a report must not write
    since = float(await db.get_setting("ledger_since", "0") or 0)
    tokens = await db.scalar(
        "SELECT COALESCE(SUM(json_extract(data_json, '$.tokens')), 0) FROM events WHERE type = 'tokens' AND ts >= ?",
        (since,))

    away = await db.get_away_since()
    since_away: dict[str, Any] = {"away_since": away}
    if away is not None:
        rows = [r for r in incidents if r["opened_at"] >= away]
        by_class: dict[str, dict[str, Any]] = {}
        for r in rows:
            c = by_class.setdefault(r["fault_class"] or "unknown", {"incidents": 0, "verified": 0, "unresolved": 0})
            c["incidents"] += 1
            c["verified"] += r["status"] == "verified"
            c["unresolved"] += r["status"] == "unresolved"
        since_away.update(
            incidents=len(rows),
            verified=sum(r["status"] == "verified" for r in rows),
            unresolved=sum(r["status"] == "unresolved" for r in rows),
            open=sum(r["closed_at"] is None for r in rows),
            cost_usd=round(sum(r["cost_usd"] for r in rows), 6),
            by_class=dict(sorted(by_class.items())),
        )

    return {
        "generated_at": time.time(),
        "window": {"start": start, "end": end, "mode": win["mode"]},
        "ladder": {"current": await db.get_ladder(), "timeline": timeline},
        "per_class": per_class,
        "overall": {**overall, **costs},
        "cache_hit_rate": round(cached / prompt, 4) if prompt else None,
        "cache": {"prompt_tokens": prompt, "cached_tokens": cached},
        "promotions": ladder_rows,
        "proving": proving,
        "heldout_classes": heldout,
        "generalization": {"unseen_seeds_proving": pass_total(proving_rows),
                           "unseen_classes": pass_total(heldout_rows)},
        "tokens_served": int(tokens or 0),
        "ledger_since": since,
        "since_away": since_away,
    }


def _ts(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S") if value else "-"


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def _stat_cell(s: dict) -> str:
    return f"{s['incidents']} / {_pct(s['success_rate'])} / {_usd(s['mean_cost'])}"


def _table(head: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return out + [""]


def render(data: dict[str, Any]) -> str:
    w = data["window"]
    o = data["overall"]
    L = ["# Understudy run report", "", f"Generated {_ts(data['generated_at'])}. "
         f"Run window {_ts(w['start'])} to {_ts(w['end'])}.", ""]

    L += ["## 1. Rung per class over time", ""]
    rows = []
    for name, current in sorted(data["ladder"]["current"].items()):
        path = ["super"] + [f"{e['to_rung']} ({_ts(e['ts'])[11:]})" for e in data["ladder"]["timeline"].get(name, [])]
        rows.append([name, " -> ".join(path), current])
    L += _table(["class", "path", "current"], rows)

    L += ["## 2. First vs last 30 minutes", ""]
    if w["mode"] == "halves":
        L += ["The run is shorter than 60 minutes, so the window is split in halves (first half vs second half).", ""]
    elif w["mode"] == "empty":
        L += ["No incidents recorded.", ""]
    L += ["Cells are closed incidents / success rate / mean incident cost. Unresolved incidents count as failures.", ""]
    L += _table(["class", "first", "last", "all"],
                [[n, _stat_cell(c["first"]), _stat_cell(c["last"]), _stat_cell(c["overall"])]
                 for n, c in data["per_class"].items()] +
                [["overall", _stat_cell(o["first"]), _stat_cell(o["last"]), _stat_cell(o["overall"])]])

    L += ["## 3. Cost per incident", "",
          f"- Closed incidents: {o['closed_incidents']}",
          f"- All LLM spend: {_usd(o['llm_cost_usd'])}, {_usd(o['llm_cost_per_incident'])} per closed incident "
          "(includes triage, narration, librarian and other calls)",
          f"- Incident-attributed spend: {_usd(o['attributed_cost_usd'])}, "
          f"{_usd(o['attributed_cost_per_incident'])} per closed incident", ""]
    if o["by_role"]:
        L += _table(["role", "calls", "cost"], [[r["role"], r["calls"], _usd(r["cost_usd"])] for r in o["by_role"]])

    L += ["## 4. Cache hit rate", "",
          f"{_pct(data['cache_hit_rate'])} ({data['cache']['cached_tokens']} cached of "
          f"{data['cache']['prompt_tokens']} prompt tokens)", ""]

    L += ["## 5. Promotion evidence", ""]
    if data["promotions"]:
        L += _table(["time", "class", "move", "direction", "passed", "mean cost"],
                    [[_ts(e["ts"]), e["fault_class"], f"{e['from_rung']} -> {e['to_rung']}", e["direction"],
                      f"{e['evidence']['passed']}/{e['evidence']['total']}", _usd(e["evidence"]["mean_cost_usd"])]
                     for e in data["promotions"]])
    else:
        L += ["No ladder events.", ""]
    if data["proving"]:
        L += ["Proving-ground trials by class and rung:", ""]
        L += _table(["class", "rung", "passed", "mean cost", "mean steps"],
                    [[n, r, f"{t['passed']}/{t['total']}", _usd(t["mean_cost_usd"]), t["mean_steps"]]
                     for n, rungs in data["proving"].items() for r, t in rungs.items()])
    else:
        L += ["No proving-ground trials.", ""]

    L += ["## 6. Held-out classes", ""]
    if data["heldout_classes"]:
        for name, h in data["heldout_classes"].items():
            L += [f"### {name}", ""]
            L += _table(["time", "rung", "seed", "result", "steps", "cost", "reason"],
                        [[_ts(r["ts"]), r["rung"], r["seed"], "pass" if r["passed"] else "FAIL", r["steps"],
                          _usd(r["cost_usd"]), r["reason"] or ""] for r in h["runs"]])
    else:
        L += ["No held-out class runs.", ""]
    g = data["generalization"]
    for label, key in (("Unseen seeds (proving trials)", "unseen_seeds_proving"), ("Unseen classes", "unseen_classes")):
        L.append(f"- {label}: {g[key]['passed']}/{g[key]['total']} passed ({_pct(g[key]['rate'])})")
    L.append("")

    L += ["## 7. Tokens served to the fleet", "",
          f"{data['tokens_served']} tokens since {_ts(data['ledger_since'])}", ""]

    L += ["## 8. Since away", ""]
    a = data["since_away"]
    if a["away_since"] is None:
        L += ["No away period is set.", ""]
    else:
        L += [f"Away since {_ts(a['away_since'])}: {a['incidents']} incidents, {a['verified']} verified, "
              f"{a['unresolved']} unresolved, {a['open']} still open, {_usd(a['cost_usd'])} spent.", ""]
        L += _table(["class", "incidents", "verified", "unresolved"],
                    [[n, c["incidents"], c["verified"], c["unresolved"]] for n, c in a["by_class"].items()])
    return "\n".join(L)


async def run(db_path: Path, out: Path | None, json_out: Path) -> str:
    db = await Database.open(db_path)
    try:
        data = await collect(db)
    finally:
        await db.close()
    text = render(data)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(data, indent=2, sort_keys=True))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=settings.DB_PATH)
    parser.add_argument("--out", type=Path, default=None, help="also write the markdown here")
    parser.add_argument("--json-out", type=Path, default=settings.DB_PATH.parent / "report.json")
    args = parser.parse_args()
    print(asyncio.run(run(args.db, args.out, args.json_out)))


if __name__ == "__main__":
    main()
