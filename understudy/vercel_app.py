"""Read-only API for serverless hosting.

The orchestrator and fleet need long-lived processes, subprocesses and a writable
SQLite file, so they are not started here. This app serves the stored numbers from a
snapshot of data/understudy.db when one is bundled, and idle payloads otherwise.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from pathlib import Path

import aiosqlite
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from understudy import ledger
from understudy.db import Database
from understudy.events import Ledger

DB_FILE = Path(os.environ.get("UNDERSTUDY_DB", settings.DB_PATH))
HISTORY_LIMIT = 2000
# A missing, corrupt, locked or half-written snapshot must degrade to empty payloads, never a 500.
READ_ERRORS = (aiosqlite.Error, sqlite3.Error, OSError, ValueError, KeyError, TypeError)

log = logging.getLogger("understudy.vercel")
app = FastAPI(title="understudy (read-only)")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins(), allow_methods=["GET"], allow_headers=["*"])


async def _open() -> Database | None:
    if not DB_FILE.is_file():
        return None
    try:
        # immutable=1: the bundle is read-only and a WAL file cannot be created next to it.
        conn = await aiosqlite.connect(f"file:{DB_FILE}?immutable=1", uri=True, timeout=2.0)
    except READ_ERRORS as exc:
        log.warning("snapshot unreadable: %s", type(exc).__name__)
        return None
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
    except READ_ERRORS as exc:
        log.warning("snapshot read failed: %s", type(exc).__name__)
        return idle
    finally:
        try:
            await db.close()
        except READ_ERRORS:
            pass


async def _db_readable() -> bool:
    async def probe(db: Database) -> bool:
        await db.fetchall("SELECT 1 FROM events LIMIT 1")
        return True

    return await _read(probe, False)


@app.get("/healthz")
async def healthz() -> dict:
    present = DB_FILE.is_file()
    return {"ok": True, "backend": "read-only", "db": present, "db_readable": present and await _db_readable(), "live_stream": False}


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
    since = since if math.isfinite(since) and since > 0 else 0.0

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
