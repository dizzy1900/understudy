"""Deterministic health check that decides whether an incident is fixed.

verify(): probe A, wait, a concurrent burst of 4, probe B. Every probe goes through the node's real
/v1/chat/completions proxy, so a fix only counts if the whole path serves tokens. Agents never call into
or edit this module; their tools only reach files inside the node directory.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

from config import settings
from understudy.events import Probe

PROMPT = "Reply with exactly: OK"
BURST_SIZE = 4


@dataclass
class ProbeResult:
    name: str
    ok: bool
    ms: float
    detail: str
    status: int | None = None

    def to_event(self) -> Probe:
        return Probe(name=self.name, ok=self.ok, ms=round(self.ms, 1), detail=self.detail)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "ms": round(self.ms, 1), "detail": self.detail}


@dataclass
class Verdict:
    node_id: str
    verdict: str
    probes: list[ProbeResult] = field(default_factory=list)
    note: str = ""

    @property
    def verified(self) -> bool:
        return self.verdict == "verified"

    def evidence(self) -> str:
        lines = [f"{p.name}: {'ok' if p.ok else 'FAIL'} {p.ms:.0f}ms {p.detail}" for p in self.probes]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "verdict": self.verdict,
            "probes": [p.to_dict() for p in self.probes],
            "note": self.note,
        }


def _error_text(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:120]
    if isinstance(data, dict):
        if isinstance(data.get("errors"), list) and data["errors"]:
            return str(data["errors"][0])[:160]
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:160]
        if data.get("message"):
            return str(data["message"])[:160]
    return text[:120]


class Referee:
    def __init__(
        self,
        port_of: Callable[[str], int],
        model: str | None = None,
        wait_s: float = settings.REFEREE_WAIT_S,
        latency_limit_s: float = settings.PROBE_LATENCY_LIMIT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.port_of = port_of
        self.model = model or settings.fleet_model_id()
        self.wait_s = wait_s
        self.latency_limit_s = latency_limit_s
        self._sleep = sleep

    async def probe_once(self, node_id: str, name: str = "probe", client: httpx.AsyncClient | None = None) -> ProbeResult:
        url = f"http://127.0.0.1:{self.port_of(node_id)}/v1/chat/completions"
        body = {"messages": [{"role": "user", "content": PROMPT}], "temperature": 0}
        started = time.monotonic()
        owned = client is None
        client = client or httpx.AsyncClient(timeout=httpx.Timeout(self.latency_limit_s + 5, connect=5.0))
        try:
            reply = await client.post(url, json=body)
        except httpx.ConnectError:
            return ProbeResult(name, False, (time.monotonic() - started) * 1000, "connection refused")
        except httpx.TimeoutException:
            return ProbeResult(name, False, (time.monotonic() - started) * 1000, f"no response within {self.latency_limit_s + 5:.0f}s")
        except (httpx.ReadError, httpx.RemoteProtocolError):
            return ProbeResult(name, False, (time.monotonic() - started) * 1000, "connection closed without a response")
        except httpx.HTTPError as exc:
            return ProbeResult(name, False, (time.monotonic() - started) * 1000, f"{type(exc).__name__}: {exc}"[:160])
        finally:
            if owned:
                await client.aclose()
        ms = (time.monotonic() - started) * 1000
        if reply.status_code != 200:
            return ProbeResult(name, False, ms, f"HTTP {reply.status_code}: {_error_text(reply.content)}", reply.status_code)
        try:
            data = reply.json()
            choice = data["choices"][0]
            content = (choice.get("message") or {}).get("content")
            finish = choice.get("finish_reason")
            served_by = data.get("model")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return ProbeResult(name, False, ms, "HTTP 200 with a body that is not a chat completion", 200)
        if not content or not str(content).strip():
            return ProbeResult(name, False, ms, f"empty content (finish_reason={finish})", 200)
        if finish == "length":
            return ProbeResult(name, False, ms, "completion truncated (finish_reason=length)", 200)
        if served_by != self.model:
            return ProbeResult(name, False, ms, f"response model {served_by!r} is not the canonical {self.model!r}", 200)
        if ms >= self.latency_limit_s * 1000:
            return ProbeResult(name, False, ms, f"latency {ms / 1000:.1f}s is over the {self.latency_limit_s:.0f}s limit", 200)
        return ProbeResult(name, True, ms, "ok", 200)

    async def verify(self, node_id: str) -> Verdict:
        probes: list[ProbeResult] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.latency_limit_s + 5, connect=5.0)) as client:
            first = await self.probe_once(node_id, "probe_a", client)
            probes.append(first)
            if not first.ok:
                return Verdict(node_id, "rejected", probes, f"probe A failed: {first.detail}")
            await self._sleep(self.wait_s)
            burst = await asyncio.gather(
                *(self.probe_once(node_id, f"burst_{i}", client) for i in range(1, BURST_SIZE + 1))
            )
            probes.extend(burst)
            probes.append(await self.probe_once(node_id, "probe_b", client))
        failed = [p for p in probes if not p.ok]
        if failed:
            names = ", ".join(p.name for p in failed)
            return Verdict(node_id, "rejected", probes, f"{len(failed)} of {len(probes)} probes failed ({names}): {failed[0].detail}")
        return Verdict(node_id, "verified", probes, f"all {len(probes)} probes passed")


def for_supervisor(supervisor, **kwargs: Any) -> Referee:
    return Referee(supervisor.port_of, **kwargs)


def _default() -> Referee:
    from understudy.fleet.supervisor import Supervisor

    return for_supervisor(Supervisor())


async def probe_once(node_id: str, name: str = "probe") -> ProbeResult:
    return await _default().probe_once(node_id, name)


async def verify(node_id: str) -> Verdict:
    return await _default().verify(node_id)
