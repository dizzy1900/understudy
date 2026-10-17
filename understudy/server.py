"""HTTP and WebSocket server for the UI."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
from understudy import ledger
from understudy.agents import narrator
from understudy.agents.chaos import ChaosError
from understudy.events import Envelope

if TYPE_CHECKING:
    from understudy.orchestrator import Orchestrator

WEB_DIST = settings.ROOT / "web" / "dist"
HISTORY_LIMIT = 2000


class ChaosRequest(BaseModel):
    command: str


class InjectRequest(BaseModel):
    node_id: str
    fault_class: str


class StormRequest(BaseModel):
    rows: list[int] | int | None = None


def _fail(exc: ChaosError) -> HTTPException:
    return HTTPException(exc.status, exc.message)


def create_app(orch: "Orchestrator", web_dist: Path | None = WEB_DIST) -> FastAPI:
    app = FastAPI(title="understudy")
    app.state.orch = orch
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket) -> None:
        await sock.accept()
        with orch.bus.subscribe() as sub:
            await sock.send_text(Envelope.of(orch.state.snapshot()).to_json())
            await sock.send_text(Envelope.of(await ledger.compute(orch.db)).to_json())

            async def watch_client(scope: anyio.CancelScope) -> None:
                try:
                    while True:
                        await sock.receive_text()
                except WebSocketDisconnect:
                    scope.cancel()

            try:
                async with anyio.create_task_group() as group:
                    group.start_soon(watch_client, group.cancel_scope)
                    async for envelope in sub:
                        await sock.send_text(envelope.to_json())
            except (WebSocketDisconnect, RuntimeError):
                pass

    @app.get("/api/ledger")
    async def get_ledger() -> dict:
        return (await ledger.compute(orch.db)).model_dump(mode="json")

    @app.get("/api/ladder")
    async def get_ladder() -> dict:
        return {"ladder": await orch.db.get_ladder(), "events": await ledger.ladder_events(orch.db)}

    @app.get("/api/skills")
    async def get_skills() -> list[dict]:
        return await ledger.skills(orch.db)

    @app.get("/api/history")
    async def get_history(since: float = 0.0) -> dict:
        if since > 0:
            rows = await orch.db.events_since(since, HISTORY_LIMIT)
        else:
            rows = list(reversed(await orch.db.fetchall("SELECT * FROM events ORDER BY id DESC LIMIT ?", (HISTORY_LIMIT,))))
            for row in rows:
                row["data"] = json.loads(row.pop("data_json"))
        return {"events": [{"type": r["type"], "ts": r["ts"], "data": r["data"]} for r in rows]}

    @app.get("/api/voice/status")
    async def voice_status() -> dict:
        return {"available": False, "model": settings.MODELS["omni"].id, "reason": "voice input is not implemented yet"}

    @app.post("/api/chaos")
    async def post_chaos(body: ChaosRequest) -> dict:
        try:
            return {"injected": await orch.chaos.command(body.command)}
        except ChaosError as exc:
            raise _fail(exc) from None

    @app.post("/api/inject")
    async def post_inject(body: InjectRequest) -> dict:
        try:
            return {"injected": [await orch.chaos.inject(body.node_id, body.fault_class)]}
        except ChaosError as exc:
            raise _fail(exc) from None

    @app.post("/api/storm")
    async def post_storm(body: StormRequest | None = None) -> dict:
        try:
            return {"injected": await orch.chaos.storm(body.rows if body else None)}
        except ChaosError as exc:
            raise _fail(exc) from None

    @app.post("/api/away")
    async def post_away() -> dict:
        now = time.time()
        await orch.db.set_away_since(now)
        return {"away_since": now}

    @app.post("/api/back")
    async def post_back() -> dict:
        since = await orch.db.get_away_since()
        if since is None:
            since = await ledger.ledger_since(orch.db)
        text = await narrator.briefing(orch.db, since)
        await orch.db.set_away_since(None)
        await orch.ctx.say(text, "high", polish=False)
        return {"since": since, "briefing": text, "ledger": (await ledger.compute(orch.db)).model_dump(mode="json")}

    @app.post("/api/voice")
    async def post_voice(request: Request) -> dict:
        await request.body()
        raise HTTPException(501, "voice input is not implemented yet")

    @app.post("/api/promote/run")
    async def post_promote() -> dict[str, Any]:
        if orch.promoter is None:
            return {"started": False, "reason": "the promoter is not running"}
        return await orch.promoter.run_now()

    @app.post("/api/demo/reset")
    async def post_reset() -> dict:
        if not settings.DEMO_MODE:
            raise HTTPException(403, "demo reset is disabled; start with DEMO_MODE=1 to allow it")
        await orch.reset_demo()
        return {"ok": True}

    if web_dist is not None and web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    return app
