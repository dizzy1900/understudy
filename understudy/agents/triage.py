"""Classifies an incident symptom into a fault class."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings
from understudy import llm
from understudy.context import Context

log = logging.getLogger("understudy.triage")

CONFIDENCE_FLOOR = 0.6
LOG_LINES = 30


@dataclass
class Triage:
    fault_class: str
    confidence: float
    model: str


def _signatures(skills: list[dict]) -> dict[str, str]:
    return {row["fault_class"]: str((row.get("card") or {}).get("signature", "")).strip() for row in skills}


def build_prompt(symptom: str, log_lines: list[str], skills: list[dict]) -> list[dict[str, str]]:
    known = _signatures(skills)
    classes = []
    for name in settings.TRAINING_FAULT_CLASSES:
        sig = known.get(name)
        classes.append(f"- {name}: {sig}" if sig else f"- {name}")
    system = (
        "You classify an incident on an inference node into one fault class. Classes:\n"
        + "\n".join(classes)
        + '\n- unknown: none of the above fits\n\nReply with one JSON object only: '
        '{"fault_class": "<class>", "confidence": <0 to 1>}'
    )
    tail = "\n".join(log_lines[-LOG_LINES:]) or "(no log lines)"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Symptom: {symptom}\n\nLast log lines:\n{tail}"},
    ]


def parse_reply(text: str) -> tuple[str, float] | None:
    obj = llm.parse_action(text)
    if not obj:
        return None
    name = obj.get("fault_class")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if name not in settings.TRAINING_FAULT_CLASSES:
        name = "unknown"
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return name, max(0.0, min(1.0, confidence))


async def _ask(ctx: Context, model_key: str, messages: list[dict[str, str]], meta: dict) -> tuple[str, float] | None:
    result = await ctx.chat("triage", model_key, messages, thinking=False, max_tokens=120, temperature=0.0, meta=meta)
    return parse_reply(result.content)


async def classify(
    ctx: Context, symptom: str, log_lines: list[str], skills: list[dict], incident_id: int | None = None
) -> Triage:
    """NANO first; unknown or low confidence goes to SUPER. BudgetExceeded propagates."""
    messages = build_prompt(symptom, log_lines, skills)
    meta = {"incident_id": incident_id}
    first = None
    try:
        first = await _ask(ctx, "nano", messages, meta)
    except llm.BudgetExceeded:
        raise
    except Exception as exc:
        log.warning("nano triage failed: %r", exc)
    if first is not None and first[0] != "unknown" and first[1] >= CONFIDENCE_FLOOR:
        return Triage(first[0], first[1], "nano")
    try:
        second = await _ask(ctx, "super", messages, meta)
    except llm.BudgetExceeded:
        raise
    except Exception as exc:
        log.warning("super triage failed: %r", exc)
        second = None
    if second is not None:
        return Triage(second[0], second[1], "super")
    if first is not None:
        return Triage(first[0], first[1], "nano")
    return Triage("unknown", 0.0, "none")
