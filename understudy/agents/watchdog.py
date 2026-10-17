"""Decides when a responder is stuck. Code only, no model calls."""
from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Callable

REPEAT_LIMIT = 3
REPEAT_WINDOW = 6


class Watchdog:
    """Trips on the same action and arguments three times within the last six calls, on an exhausted step
    budget, or when the responder has made no progress for stall_s seconds. Progress is a tool call that
    finished."""

    def __init__(
        self,
        max_steps: int = 14,
        stall_s: float = 90.0,
        repeat_limit: int = REPEAT_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_steps = max_steps
        self.stall_s = stall_s
        self.repeat_limit = repeat_limit
        self._clock = clock
        self._recent: deque[str] = deque(maxlen=REPEAT_WINDOW)
        self.steps = 0
        self.last_progress = clock()

    @staticmethod
    def _key(action: str, args: dict[str, Any]) -> str:
        return action + json.dumps(args, sort_keys=True, default=str)

    def before_step(self) -> str | None:
        if self.steps >= self.max_steps:
            return f"step budget of {self.max_steps} used without a verified fix"
        return None

    def stall_deadline(self) -> float:
        """Seconds left before the no-progress limit trips."""
        return self.stall_s - (self._clock() - self.last_progress)

    def stalled(self) -> str | None:
        if self.stall_deadline() <= 0:
            return f"no progress for {self.stall_s:.0f}s"
        return None

    def is_repeat(self, action: str, args: dict[str, Any]) -> bool:
        """True when this exact call is already among the recent ones."""
        return action != "claim_fixed" and self._key(action, args) in self._recent

    def record(self, action: str, args: dict[str, Any]) -> str | None:
        """Call when a tool call has finished. Returns the reason to escalate, if any."""
        self.steps += 1
        self.last_progress = self._clock()
        if action == "claim_fixed":
            self._recent.clear()
            return None
        key = self._key(action, args)
        self._recent.append(key)
        if sum(1 for k in self._recent if k == key) >= self.repeat_limit:
            return f"{action} repeated {self.repeat_limit} times with the same arguments"
        return None

    def reset_progress(self) -> None:
        self.last_progress = self._clock()
