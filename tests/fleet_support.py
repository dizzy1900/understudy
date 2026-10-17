"""Helpers for fleet tests: a local stand-in for the upstream API and node factories."""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import settings
from understudy.fleet.nodeconfig import dump_config, load_config_dict
from understudy.fleet.supervisor import Supervisor

ALLOWED_PARAMS = {
    "model", "messages", "max_tokens", "temperature", "chat_template_kwargs", "top_p", "top_k", "seed", "stop",
}
TEST_MODEL = "test/Canonical-Model-1B"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ThreadedServer:
    def __init__(self, app: FastAPI, port: int | None = None):
        self.app = app
        self.port = port or free_port()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


class FakeUpstream(ThreadedServer):
    """Mimics the parts of the Crusoe API the fleet depends on, including its error shapes."""

    def __init__(self, model: str = TEST_MODEL, delay: float = 0.0):
        super().__init__(FastAPI())
        self.model = model
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.app.post("/v1/chat/completions")(self._chat)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def _chat(self, request: Request) -> JSONResponse:
        import asyncio

        body = await request.json()
        self.requests.append({"body": body, "auth": request.headers.get("authorization")})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        if request.headers.get("authorization") != "Bearer test-key":
            return JSONResponse({"errors": ["Authentication failed"]}, status_code=401)
        for key in body:
            if key not in ALLOWED_PARAMS:
                return JSONResponse(
                    {"errors": [f"Request blocked: parameter '{key}' is not allowed"], "id": "x"}, status_code=403
                )
        if body.get("model") != self.model:
            return JSONResponse(
                {"code": "model_not_found", "message": f"Model not found: {body.get('model')}", "error_id": "x"},
                status_code=404,
            )
        thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking")
        starved = thinking and int(body.get("max_tokens", 0)) < 64
        content = None if starved else "OK"
        return JSONResponse(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "length" if starved else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        )


def make_supervisor(tmp: Path, count: int, upstream_url: str, model: str = TEST_MODEL, prefix: str = "t") -> Supervisor:
    specs = [
        settings.NodeSpec(f"{prefix}-{i:02d}", 1, i, "live", free_port()) for i in range(1, count + 1)
    ]
    sup = Supervisor(specs=specs, nodes_dir=tmp / "nodes", model=model)
    for spec in specs:
        paths = sup.ensure_node(spec.id)
        cfg = load_config_dict(paths.config)
        cfg["upstream_base_url"] = upstream_url
        paths.config.write_text(dump_config(cfg))
    return sup


def chat_body(text: str = "Reply with exactly: OK") -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


async def post_chat(client: httpx.AsyncClient, port: int, text: str = "Reply with exactly: OK") -> httpx.Response:
    return await client.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=chat_body(text), timeout=30)
