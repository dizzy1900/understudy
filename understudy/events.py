"""Event contract for the /ws stream and an in-process pub/sub bus."""
from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

NodeZone = Literal["live", "proving"]
NodeState = Literal["healthy", "degraded", "down", "repairing"]
Severity = Literal["low", "high"]
AgentRole = Literal[
    "commander", "responder", "verifier", "watchdog", "librarian", "promoter", "narrator", "chaos", "triage"
]
AgentRung = Literal["super", "lightning", "script", "ultra", "nano"]
Rung = Literal["super", "lightning", "script"]
Verdict = Literal["verified", "rejected"]
Direction = Literal["promote", "demote"]
Priority = Literal["low", "high"]

THOUGHT_MAX = 140

EVENT_TYPES: dict[str, type["EventData"]] = {}


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventData(Payload):
    event_type: ClassVar[str]


def event(name: str):
    def register(cls: type[EventData]) -> type[EventData]:
        cls.event_type = name
        EVENT_TYPES[name] = cls
        return cls

    return register


class NodeInfo(Payload):
    id: str
    row: int
    col: int
    zone: NodeZone
    status: NodeState
    tokens_per_min: float = 0.0
    last_probe_ms: float = 0.0


@event("fleet_snapshot")
class FleetSnapshot(EventData):
    nodes: list[NodeInfo]


@event("node_status")
class NodeStatus(EventData):
    node_id: str
    status: NodeState
    reason: str | None = None


@event("tokens")
class Tokens(EventData):
    node_id: str
    tokens: int


@event("incident_opened")
class IncidentOpened(EventData):
    incident_id: int
    node_id: str
    symptom: str
    severity: Severity


@event("agent_spawned")
class AgentSpawned(EventData):
    agent_id: str
    role: AgentRole
    model: str
    rung: AgentRung
    incident_id: int | None = None
    node_id: str | None = None


@event("agent_thought")
class AgentThought(EventData):
    agent_id: str
    text: str

    @field_validator("text")
    @classmethod
    def _clip(cls, value: str) -> str:
        if len(value) <= THOUGHT_MAX:
            return value
        return value[: THOUGHT_MAX - 3] + "..."


@event("tool_call")
class ToolCall(EventData):
    agent_id: str
    tool: str
    args_summary: str
    ok: bool
    result_summary: str


@event("fix_claimed")
class FixClaimed(EventData):
    incident_id: int
    agent_id: str
    summary: str


class Probe(Payload):
    name: str
    ok: bool
    ms: float
    detail: str = ""


@event("verification")
class Verification(EventData):
    incident_id: int
    node_id: str
    verdict: Verdict
    probes: list[Probe]
    quality: int | None = Field(default=None, ge=1, le=5)
    note: str = ""


@event("escalation")
class Escalation(EventData):
    incident_id: int
    from_rung: Rung
    to_rung: Rung
    reason: str


@event("incident_closed")
class IncidentClosed(EventData):
    incident_id: int
    node_id: str
    fault_class: str
    rung: Rung
    steps: int
    cost_usd: float
    seconds: float


@event("skill_updated")
class SkillUpdated(EventData):
    fault_class: str
    version: int
    summary: str


class LadderEvidence(Payload):
    passed: int
    total: int
    mean_cost_usd: float


@event("ladder_update")
class LadderUpdate(EventData):
    fault_class: str
    from_rung: Rung
    to_rung: Rung
    direction: Direction
    evidence: LadderEvidence


@event("narration")
class Narration(EventData):
    text: str
    priority: Priority = "low"


class RungStat(Payload):
    incidents: int = 0
    cost_usd: float = 0.0


@event("ledger")
class Ledger(EventData):
    since: float
    incidents_total: int
    verified: int
    rejected_claims: int
    promotions: int
    demotions: int
    cost_total_usd: float
    cost_per_incident_usd: float
    tokens_served: int
    cache_hit_rate: float
    by_rung: dict[Rung, RungStat]
    ladder: dict[str, Rung]


@event("agent_done")
class AgentDone(EventData):
    agent_id: str


class Envelope(BaseModel):
    """Wire format: {"type", "ts", "data"}. ts is unix seconds."""

    type: str
    ts: float
    data: dict[str, Any]

    @classmethod
    def of(cls, payload: EventData, ts: float | None = None) -> "Envelope":
        return cls(
            type=payload.event_type,
            ts=time.time() if ts is None else ts,
            data=payload.model_dump(mode="json", exclude_none=True),
        )

    def payload(self) -> EventData:
        return EVENT_TYPES[self.type].model_validate(self.data)

    def to_json(self) -> str:
        return self.model_dump_json()


class Subscription:
    """A bounded queue of envelopes. When full, the oldest envelope is dropped."""

    def __init__(self, bus: "EventBus", maxsize: int):
        self._bus = bus
        self._queue: asyncio.Queue[Envelope] = asyncio.Queue(maxsize)
        self.dropped = 0

    def put(self, envelope: Envelope) -> None:
        if self._queue.full():
            self._queue.get_nowait()
            self.dropped += 1
        self._queue.put_nowait(envelope)

    async def get(self) -> Envelope:
        return await self._queue.get()

    def close(self) -> None:
        self._bus._subscribers.discard(self)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Envelope:
        return await self.get()

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    """Fan-out to any number of subscribers.

    publish() is synchronous and never blocks, so a stalled websocket client
    cannot back-pressure the agents. Call it from the event loop's thread.
    """

    def __init__(self, queue_size: int = 1000):
        self._queue_size = queue_size
        self._subscribers: set[Subscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, maxsize: int | None = None) -> Subscription:
        sub = Subscription(self, maxsize or self._queue_size)
        self._subscribers.add(sub)
        return sub

    def publish(self, payload: EventData, ts: float | None = None) -> Envelope:
        envelope = Envelope.of(payload, ts)
        for sub in tuple(self._subscribers):
            sub.put(envelope)
        return envelope
