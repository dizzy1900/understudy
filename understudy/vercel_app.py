"""Read-only API for serverless hosting.

The orchestrator and fleet need long-lived processes, subprocesses and a writable
SQLite file, so they are not started here. This app serves the stored numbers from a
snapshot of data/understudy.db when one is bundled, and idle payloads otherwise.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiosqlite
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import settings
from understudy import ledger
from understudy.db import Database
from understudy.events import Ledger

DB_FILE = Path(os.environ.get("UNDERSTUDY_DB", settings.DB_PATH))
HISTORY_LIMIT = 2000

app = FastAPI(title="understudy (read-only)")


async def _open() -> Database | None:
    if not DB_FILE.is_file():
        return None
    # immutable=1: the bundle is read-only and a WAL file cannot be created next to it.
    conn = await aiosqlite.connect(f"file:{DB_FILE}?immutable=1", uri=True)
    conn.row_factory = aiosqlite.Row
    return Database(conn)


def _idle_ledger() -> dict:
    return Ledger(
        since=time.time(),
        incidents_total=0,
        verified=0,
        rejected_claims=0,
        promotions=0,
        demotions=0,
        cost_total_usd=0.0,
        cost_per_incident_usd=0.0,
        tokens_served=0,
        cache_hit_rate=0.0,
        by_rung={},  # type: ignore[arg-type]
        ladder={},
    ).model_dump(mode="json")


async def _read(fn, idle):
    db = await _open()
    if db is None:
        return idle
    try:
        return await fn(db)
    except aiosqlite.Error:
        return idle
    finally:
        await db.close()


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "backend": "read-only", "db": DB_FILE.is_file()}


@app.get("/api/ledger")
async def get_ledger() -> dict:
    async def run(db: Database) -> dict:
        since = await db.get_setting("ledger_since")
        if since is None:
            return _idle_ledger()
        return (await ledger.compute(db)).model_dump(mode="json")

    return await _read(run, _idle_ledger())


@app.get("/api/ladder")
async def get_ladder() -> dict:
    async def run(db: Database) -> dict:
        return {"ladder": await db.get_ladder(), "events": await ledger.ladder_events(db)}

    return await _read(run, {"ladder": {}, "events": []})


@app.get("/api/skills")
async def get_skills() -> list[dict]:
    async def run(db: Database) -> list[dict]:
        return await ledger.skills(db)

    return await _read(run, [])


@app.get("/api/history")
async def get_history(since: float = 0.0) -> dict:
    async def run(db: Database) -> dict:
        if since > 0:
            rows = await db.events_since(since, HISTORY_LIMIT)
            return {"events": [{"type": r["type"], "ts": r["ts"], "data": r["data"]} for r in rows]}
        rows = list(reversed(await db.fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (HISTORY_LIMIT,))))
        return {"events": [{"type": r["type"], "ts": r["ts"], "data": json.loads(r["data_json"])} for r in rows]}

    return await _read(run, {"events": []})


@app.api_route("/ws", methods=["GET"])
async def ws_unavailable() -> JSONResponse:
    return JSONResponse({"detail": "the live stream needs the orchestrator host; serverless functions cannot hold WebSockets"}, status_code=501)
