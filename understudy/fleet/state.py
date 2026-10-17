"""In-memory view of the fleet that the monitor, traffic generator and orchestrator share."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable

from config import settings
from understudy.events import EventBus, FleetSnapshot, NodeInfo, NodeState, NodeStatus

TOKEN_WINDOW_S = 60.0


@dataclass
class NodeView:
    spec: settings.NodeSpec
    status: NodeState = "healthy"
    reason: str | None = None
    last_probe_ms: float = 0.0
    tokens_total: int = 0
    window: deque = field(default_factory=deque)


class FleetState:
    def __init__(
        self,
        specs: Iterable[settings.NodeSpec] | None = None,
        bus: EventBus | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.bus = bus
        self._clock = clock
        self.nodes = {spec.id: NodeView(spec) for spec in (specs if specs is not None else settings.NODES)}

    def status(self, node_id: str) -> NodeState:
        return self.nodes[node_id].status

    def set_status(self, node_id: str, status: NodeState, reason: str | None = None) -> bool:
        """Records the status and publishes node_status when it changed. Returns True on a change."""
        view = self.nodes[node_id]
        changed = view.status != status
        view.status = status
        view.reason = reason
        if changed and self.bus is not None:
            self.bus.publish(NodeStatus(node_id=node_id, status=status, reason=reason))
        return changed

    def mark_repairing(self, node_id: str, reason: str | None = None) -> None:
        self.set_status(node_id, "repairing", reason)

    def set_probe(self, node_id: str, ms: float) -> None:
        self.nodes[node_id].last_probe_ms = ms

    def record_tokens(self, node_id: str, tokens: int) -> None:
        view = self.nodes[node_id]
        view.tokens_total += tokens
        view.window.append((self._clock(), tokens))

    def tokens_per_min(self, node_id: str) -> float:
        view = self.nodes[node_id]
        cutoff = self._clock() - TOKEN_WINDOW_S
        while view.window and view.window[0][0] < cutoff:
            view.window.popleft()
        return float(sum(tokens for _ts, tokens in view.window))

    def healthy_live(self) -> list[str]:
        return [n for n, v in self.nodes.items() if v.spec.zone == "live" and v.status == "healthy"]

    def snapshot(self) -> FleetSnapshot:
        return FleetSnapshot(
            nodes=[
                NodeInfo(
                    id=v.spec.id,
                    row=v.spec.row,
                    col=v.spec.col,
                    zone=v.spec.zone,
                    status=v.status,
                    tokens_per_min=self.tokens_per_min(v.spec.id),
                    last_probe_ms=round(v.last_probe_ms, 1),
                )
                for v in self.nodes.values()
            ]
        )
