"""Scripted stand-ins for the model and the referee, and a context wired to a temporary fleet."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from config import settings
from understudy.agents.verifier import Verifier
from understudy.context import Context
from understudy.db import Database
from understudy.events import EventBus
from understudy.fleet.referee import ProbeResult, Verdict
from understudy.fleet.state import FleetState
from understudy.fleet.supervisor import Supervisor
from understudy.llm import LLMResult


def result(content: str, cost: float = 0.001, cached: int = 0) -> LLMResult:
    return LLMResult(
        content=content, reasoning="", finish_reason="stop", prompt_tokens=100, completion_tokens=20,
        cached_tokens=cached, cost_usd=cost, latency_ms=5.0,
    )


def turn(action: str, thought: str = "Checking.", **args: Any) -> str:
    return json.dumps({"thought": thought, "action": action, "args": args})


class ScriptedChat:
    """Replies come from per-role queues. A queue entry is a string, an LLMResult, an exception, or a callable
    taking the message list."""

    def __init__(self, **queues: list[Any]):
        self.queues = {role: list(items) for role, items in queues.items()}
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, role, model_key, messages, *, thinking, max_tokens, temperature=0.6, meta=None):
        self.calls.append(
            {"role": role, "model_key": model_key, "messages": [dict(m) for m in messages], "thinking": thinking, "meta": meta}
        )
        queue = self.queues.get(role)
        if not queue:
            raise AssertionError(f"no scripted reply left for role {role!r}")
        item = queue.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, LLMResult) else result(item)

    def of(self, role: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["role"] == role]


def verdict(node_id: str, ok: bool, detail: str = "HTTP 404: model_not_found") -> Verdict:
    if ok:
        probes = [ProbeResult(n, True, 20.0, "ok") for n in ("probe_a", "burst_1", "burst_2", "burst_3", "burst_4", "probe_b")]
        return Verdict(node_id, "verified", probes, "all 6 probes passed")
    probes = [ProbeResult("probe_a", False, 12.0, detail)]
    return Verdict(node_id, "rejected", probes, f"probe A failed: {detail}")


class FakeReferee:
    def __init__(self, verdicts: list[bool] | None = None, detail: str = "HTTP 404: model_not_found"):
        self.verdicts = list(verdicts or [])
        self.detail = detail
        self.verified: list[str] = []

    async def probe_once(self, node_id: str, name: str = "probe"):
        return ProbeResult(name, True, 10.0, "ok")

    async def verify(self, node_id: str) -> Verdict:
        self.verified.append(node_id)
        ok = self.verdicts.pop(0) if self.verdicts else True
        return verdict(node_id, ok, self.detail)


NODE = "r1-01"
OPEN: list[Database] = []


async def make_context(tmp: Path, chat: ScriptedChat, referee: FakeReferee | None = None, **overrides: Any) -> Context:
    db = await Database.open(tmp / "t.db")
    OPEN.append(db)
    specs = [settings.NodeSpec(NODE, 1, 1, "live", 19101)]
    sup = Supervisor(specs=specs, nodes_dir=tmp / "nodes")
    sup.ensure_all()
    bus = EventBus()
    ctx = Context(
        db, bus, sup, FleetState(specs, bus), referee or FakeReferee(), chat=chat,  # type: ignore[arg-type]
        **overrides,
    )
    ctx.verifier = Verifier(ctx)
    return ctx


def collect(bus: EventBus) -> Callable[[], list[Any]]:
    """Returns a function that drains everything published so far."""
    sub = bus.subscribe(maxsize=5000)
    seen: list[Any] = []

    def drain() -> list[Any]:
        while not sub._queue.empty():
            seen.append(sub._queue.get_nowait())
        return seen

    return drain
