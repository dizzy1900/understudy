"""Sends each healthy live node a small real request on a fixed cadence and publishes what it served."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Callable

import httpx

from config import settings
from understudy.events import EventBus, Tokens
from understudy.fleet.state import FleetState

log = logging.getLogger("understudy.traffic")

PROMPTS = (
    "Name three uses of a hash map in one sentence.",
    "Give a one-line definition of idempotency.",
    "What does a load balancer do? Answer in under 20 words.",
    "Suggest a short name for a log-rotation script.",
    "Explain TCP backoff in one sentence.",
    "List two differences between a process and a thread.",
    "Write a haiku about a quiet server room.",
    "What is a cache stampede? One sentence.",
    "Summarize what a checksum is in ten words.",
    "Give one reason to pin a dependency version.",
)


class Traffic:
    def __init__(
        self,
        state: FleetState,
        port_of: Callable[[str], int],
        bus: EventBus | None = None,
        interval_s: float = settings.TRAFFIC_INTERVAL_S,
        jitter: float = 0.3,
        snapshot_interval_s: float = settings.SNAPSHOT_INTERVAL_S,
        refresh: Callable[[], None] | None = None,
        rng: random.Random | None = None,
    ):
        self.state = state
        self.port_of = port_of
        self.bus = bus
        self.interval_s = interval_s
        self.jitter = jitter
        self.snapshot_interval_s = snapshot_interval_s
        self.refresh = refresh
        self.rng = rng or random.Random()
        self.sent = 0
        self.failed = 0
        self._tasks: list[asyncio.Task] = []
        self._client: httpx.AsyncClient | None = None

    async def send_one(self, node_id: str) -> int | None:
        """One chat request through the node's proxy. Returns total tokens served, None on failure."""
        assert self._client is not None
        body = {"messages": [{"role": "user", "content": self.rng.choice(PROMPTS)}], "temperature": 0.7}
        self.sent += 1
        try:
            reply = await self._client.post(f"http://127.0.0.1:{self.port_of(node_id)}/v1/chat/completions", json=body)
            if reply.status_code != 200:
                raise ValueError(f"HTTP {reply.status_code}")
            tokens = int((reply.json().get("usage") or {}).get("total_tokens") or 0)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.failed += 1
            log.debug("traffic to %s failed: %s", node_id, exc)
            return None
        self.state.record_tokens(node_id, tokens)
        if self.bus is not None:
            self.bus.publish(Tokens(node_id=node_id, tokens=tokens))
        return tokens

    async def _node_loop(self, node_id: str) -> None:
        await asyncio.sleep(self.rng.uniform(0, self.interval_s))
        while True:
            if self.state.status(node_id) == "healthy":
                await self.send_one(node_id)
            await asyncio.sleep(self.interval_s * self.rng.uniform(1 - self.jitter, 1 + self.jitter))

    async def _snapshot_loop(self) -> None:
        while True:
            if self.refresh is not None:
                self.refresh()
            if self.bus is not None:
                self.bus.publish(self.state.snapshot())
            await asyncio.sleep(self.snapshot_interval_s)

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0))
        live = [n for n, v in self.state.nodes.items() if v.spec.zone == "live"]
        self._tasks = [asyncio.create_task(self._node_loop(n)) for n in live]
        self._tasks.append(asyncio.create_task(self._snapshot_loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        if self._client is not None:
            await self._client.aclose()
            self._client = None
