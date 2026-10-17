"""SQLite schema and typed helpers (WAL)."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT 'live',
    symptom TEXT NOT NULL,
    severity TEXT NOT NULL,
    fault_class TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    rung TEXT,
    escalations INTEGER NOT NULL DEFAULT 0,
    steps INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    quality INTEGER,
    opened_at REAL NOT NULL,
    closed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    ok INTEGER NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_incident ON actions(incident_id);

CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    probes_json TEXT NOT NULL,
    quality INTEGER,
    note TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_incident ON verifications(incident_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    model_key TEXT NOT NULL,
    model TEXT NOT NULL,
    incident_id INTEGER,
    agent_id TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    finish_reason TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    retries INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_llm_calls_incident ON llm_calls(incident_id);

CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fault_class TEXT NOT NULL,
    version INTEGER NOT NULL,
    card_json TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (fault_class, version)
);

CREATE TABLE IF NOT EXISTS ladder (
    fault_class TEXT PRIMARY KEY,
    rung TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ladder_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    fault_class TEXT NOT NULL,
    from_rung TEXT NOT NULL,
    to_rung TEXT NOT NULL,
    direction TEXT NOT NULL,
    passed INTEGER NOT NULL,
    total INTEGER NOT NULL,
    mean_cost_usd REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS heldout_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    fault_class TEXT NOT NULL,
    rung TEXT NOT NULL,
    seed INTEGER,
    node_id TEXT,
    passed INTEGER NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_heldout_class ON heldout_reports(fault_class, rung);

-- Ground truth for injected faults. Responder tools never read this table.
CREATE TABLE IF NOT EXISTS faults_truth (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT 'live',
    fault_class TEXT NOT NULL,
    seed INTEGER NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    incident_id INTEGER,
    injected_at REAL NOT NULL,
    cleared_at REAL
);
CREATE INDEX IF NOT EXISTS idx_faults_node ON faults_truth(node_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INCIDENT_FIELDS = frozenset(
    {"fault_class", "status", "rung", "escalations", "steps", "cost_usd", "quality", "closed_at"}
)
Row = dict[str, Any]


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Database:
    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    @classmethod
    async def open(cls, path: Path | str | None = None) -> "Database":
        target = Path(path) if path is not None else settings.DB_PATH
        if str(target) != ":memory:":
            target.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(target)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.executescript(SCHEMA)
        db = cls(conn)
        now = time.time()
        await conn.executemany(
            "INSERT OR IGNORE INTO ladder (fault_class, rung, updated_at) VALUES (?, 'super', ?)",
            [(name, now) for name in settings.TRAINING_FAULT_CLASSES],
        )
        await conn.commit()
        return db

    async def close(self) -> None:
        await self.conn.close()

    async def execute(self, sql: str, params: tuple | list = ()) -> int:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor.lastrowid or 0

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[Row]:
        async with self.conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def fetchone(self, sql: str, params: tuple | list = ()) -> Row | None:
        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def scalar(self, sql: str, params: tuple | list = ()) -> Any:
        row = await self.fetchone(sql, params)
        return next(iter(row.values())) if row else None

    # settings -----------------------------------------------------------------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        value = await self.scalar("SELECT value FROM settings WHERE key = ?", (key,))
        return default if value is None else value

    async def set_setting(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def delete_setting(self, key: str) -> None:
        await self.execute("DELETE FROM settings WHERE key = ?", (key,))

    async def get_away_since(self) -> float | None:
        value = await self.get_setting("away_since")
        return float(value) if value is not None else None

    async def set_away_since(self, ts: float | None) -> None:
        if ts is None:
            await self.delete_setting("away_since")
        else:
            await self.set_setting("away_since", repr(ts))

    # events -------------------------------------------------------------------

    async def add_event(self, ts: float, type_: str, data: dict[str, Any]) -> int:
        return await self.execute("INSERT INTO events (ts, type, data_json) VALUES (?, ?, ?)", (ts, type_, dumps(data)))

    async def events_since(self, since: float, limit: int = 5000) -> list[Row]:
        rows = await self.fetchall("SELECT * FROM events WHERE ts > ? ORDER BY id LIMIT ?", (since, limit))
        for row in rows:
            row["data"] = json.loads(row.pop("data_json"))
        return rows

    # llm_calls ----------------------------------------------------------------

    async def record_llm_call(
        self,
        *,
        role: str,
        model_key: str,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        finish_reason: str | None = None,
        ok: bool = True,
        retries: int = 0,
        error: str | None = None,
        incident_id: int | None = None,
        agent_id: str | None = None,
        meta: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int:
        return await self.execute(
            "INSERT INTO llm_calls (ts, role, model_key, model, incident_id, agent_id, prompt_tokens, completion_tokens,"
            " cached_tokens, cost_usd, latency_ms, finish_reason, ok, retries, error, meta_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time() if ts is None else ts, role, model_key, model, incident_id, agent_id, prompt_tokens,
                completion_tokens, cached_tokens, cost_usd, latency_ms, finish_reason, int(ok), retries, error,
                dumps(meta) if meta else None,
            ),
        )

    async def total_cost(self) -> float:
        return float(await self.scalar("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls") or 0.0)

    async def cache_stats(self) -> tuple[int, int]:
        row = await self.fetchone(
            "SELECT COALESCE(SUM(prompt_tokens), 0) AS prompt, COALESCE(SUM(cached_tokens), 0) AS cached"
            " FROM llm_calls WHERE ok = 1"
        )
        return (int(row["prompt"]), int(row["cached"])) if row else (0, 0)

    # incidents ----------------------------------------------------------------

    async def open_incident(
        self,
        node_id: str,
        symptom: str,
        severity: str,
        zone: str = "live",
        opened_at: float | None = None,
        status: str = "open",
    ) -> int:
        return await self.execute(
            "INSERT INTO incidents (node_id, zone, symptom, severity, status, opened_at) VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, zone, symptom, severity, status, time.time() if opened_at is None else opened_at),
        )

    async def update_incident(self, incident_id: int, **fields: Any) -> None:
        unknown = set(fields) - INCIDENT_FIELDS
        if unknown or not fields:
            raise ValueError(f"unsupported incident fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self.execute(f"UPDATE incidents SET {assignments} WHERE id = ?", (*fields.values(), incident_id))

    async def get_incident(self, incident_id: int) -> Row | None:
        return await self.fetchone("SELECT * FROM incidents WHERE id = ?", (incident_id,))

    async def open_incidents(self) -> list[Row]:
        return await self.fetchall("SELECT * FROM incidents WHERE status = 'open' ORDER BY id")

    async def add_action(
        self, incident_id: int, agent_id: str, step: int, tool: str, args: dict[str, Any], ok: bool, result: str
    ) -> int:
        return await self.execute(
            "INSERT INTO actions (incident_id, agent_id, step, tool, args_json, ok, result, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (incident_id, agent_id, step, tool, dumps(args), int(ok), result, time.time()),
        )

    async def actions_for(self, incident_id: int) -> list[Row]:
        return await self.fetchall("SELECT * FROM actions WHERE incident_id = ? ORDER BY id", (incident_id,))

    async def add_verification(
        self,
        incident_id: int,
        node_id: str,
        verdict: str,
        probes: list[dict[str, Any]],
        quality: int | None = None,
        note: str = "",
    ) -> int:
        return await self.execute(
            "INSERT INTO verifications (incident_id, node_id, verdict, probes_json, quality, note, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (incident_id, node_id, verdict, dumps(probes), quality, note, time.time()),
        )

    # ladder -------------------------------------------------------------------

    async def get_ladder(self) -> dict[str, str]:
        rows = await self.fetchall("SELECT fault_class, rung FROM ladder ORDER BY fault_class")
        return {row["fault_class"]: row["rung"] for row in rows}

    async def set_ladder(self, fault_class: str, rung: str) -> None:
        await self.execute(
            "INSERT INTO ladder (fault_class, rung, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(fault_class) DO UPDATE SET rung = excluded.rung, updated_at = excluded.updated_at",
            (fault_class, rung, time.time()),
        )

    async def add_ladder_event(
        self, fault_class: str, from_rung: str, to_rung: str, direction: str, passed: int, total: int, mean_cost_usd: float
    ) -> int:
        return await self.execute(
            "INSERT INTO ladder_events (ts, fault_class, from_rung, to_rung, direction, passed, total, mean_cost_usd)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), fault_class, from_rung, to_rung, direction, passed, total, mean_cost_usd),
        )

    # skills -------------------------------------------------------------------

    async def save_skill(self, fault_class: str, card: dict[str, Any], summary: str = "") -> int:
        """Stores the card as the next version of its class and returns that version."""
        version = int(
            await self.scalar("SELECT COALESCE(MAX(version), 0) + 1 FROM skills WHERE fault_class = ?", (fault_class,))
        )
        await self.execute(
            "INSERT INTO skills (fault_class, version, card_json, summary, created_at) VALUES (?, ?, ?, ?, ?)",
            (fault_class, version, dumps(card), summary, time.time()),
        )
        return version

    async def latest_skills(self) -> list[Row]:
        rows = await self.fetchall(
            "SELECT s.* FROM skills s JOIN (SELECT fault_class, MAX(version) AS v FROM skills GROUP BY fault_class) m"
            " ON s.fault_class = m.fault_class AND s.version = m.v ORDER BY s.fault_class"
        )
        for row in rows:
            row["card"] = json.loads(row.pop("card_json"))
        return rows

    # fault ground truth and held-out evaluation ---------------------------------

    async def add_fault_truth(
        self, node_id: str, fault_class: str, seed: int, params: dict[str, Any] | None = None, zone: str = "live"
    ) -> int:
        return await self.execute(
            "INSERT INTO faults_truth (node_id, zone, fault_class, seed, params_json, injected_at) VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, zone, fault_class, seed, dumps(params or {}), time.time()),
        )

    async def clear_fault_truth(self, node_id: str) -> None:
        """Marks every uncleared fault on the node as cleared."""
        await self.execute(
            "UPDATE faults_truth SET cleared_at = ? WHERE node_id = ? AND cleared_at IS NULL", (time.time(), node_id)
        )

    async def active_faults(self, node_id: str | None = None) -> list[Row]:
        sql = "SELECT * FROM faults_truth WHERE cleared_at IS NULL"
        params: tuple = ()
        if node_id is not None:
            sql += " AND node_id = ?"
            params = (node_id,)
        return await self.fetchall(sql + " ORDER BY id", params)

    async def link_fault_incident(self, node_id: str, incident_id: int) -> None:
        await self.execute(
            "UPDATE faults_truth SET incident_id = ? WHERE node_id = ? AND cleared_at IS NULL AND incident_id IS NULL",
            (incident_id, node_id),
        )

    async def add_heldout_report(
        self,
        kind: str,
        fault_class: str,
        rung: str,
        passed: bool,
        *,
        seed: int | None = None,
        node_id: str | None = None,
        steps: int = 0,
        cost_usd: float = 0.0,
        detail: dict[str, Any] | None = None,
    ) -> int:
        return await self.execute(
            "INSERT INTO heldout_reports (ts, kind, fault_class, rung, seed, node_id, passed, steps, cost_usd, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), kind, fault_class, rung, seed, node_id, int(passed), steps, cost_usd,
             dumps(detail) if detail else None),
        )
