"""JSON action protocol: one {thought, action, args} object per turn, parsed leniently.

Message layout, in this order, so providers can reuse the cached prefix across responders:
  system  role prompt and tool schema (fixed)
  system  skill library block (changes only when a card changes)
  user    incident context
  ...     append-only assistant / user turns
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from config import settings
from understudy.llm import parse_action

MAX_RESULT_CHARS = 3000

# (name, arguments, description). The order is part of the cached prefix.
TOOLS: tuple[tuple[str, str, str], ...] = (
    ("node_status", "", "Process state, exit code and reason, port, request counters by HTTP status, quota use."),
    ("tail_log", "n", "Last n lines (n <= 80) of logs/node.log. Adds stderr.log when the process is not running."),
    ("read_config", "rel?", "The node's config.yaml as text. With rel, any small text file in the node directory, such as exit.json or stderr.log."),
    ("write_config", "yaml_text", "Replace config.yaml with the given YAML. It is validated first and the old file is kept as a backup."),
    ("list_dir", "rel", "List a directory inside the node directory, for example \".\", \"data\" or \"logs\"."),
    ("delete_files", "glob", "Delete files matching a glob under data/ only. The newest checkpoint is always kept."),
    ("fix_permissions", "rel", "Reset permissions on a path inside the node directory (files 644, directories 755)."),
    ("restart_node", "", "Stop the node process if running, then start it. Returns the new process state."),
    ("stop_node", "", "Stop the node process."),
    ("start_node", "", "Start the node process if it is not running."),
    ("port_owner", "", "Which process is listening on the node's port, and whether the fleet started it."),
    ("kill_squatter", "pid", "Kill a stray process the fleet itself started on this node's port. Any other pid is refused."),
    ("set_port", "port", "Move the node to another port in 9200-9299. Takes effect on the next start."),
    ("list_models", "", "Model ids the upstream API serves, and the upstream base URL they were read from."),
    ("probe_once", "", "Send one chat request through the node and report status, content check and latency."),
    ("claim_fixed", "summary, root_cause", "Declare the node fixed. summary: what you changed. root_cause: why it was failing."),
)
TOOL_NAMES = tuple(name for name, _args, _desc in TOOLS)

REPROMPT = (
    'Your last reply was not one JSON object with "thought", "action" and "args". '
    "Reply again with exactly one JSON object and nothing else."
)


def tool_schema() -> str:
    lines = ["Tools. Call exactly one per turn:"]
    for name, args, desc in TOOLS:
        lines.append(f"- {name}({args}): {desc}")
    return "\n".join(lines)


def standard_config() -> str:
    return (
        f"model: {settings.fleet_model_id()}\n"
        f"upstream_base_url: {settings.CRUSOE_BASE_URL}\n"
        f"api_key_env: {settings.API_KEY_ENV}\n"
        f"max_tokens: {settings.NODE_MAX_TOKENS}\n"
        "extra_body: {chat_template_kwargs: {enable_thinking: false}}\n"
        "worker_recycle_after: 0\n"
        f"worker_concurrency: {settings.NODE_WORKER_CONCURRENCY}   # must not exceed upstream_limit ({settings.NODE_UPSTREAM_LIMIT})\n"
        "retry_backoff: true"
    )


def system_prompt() -> str:
    return (
        "You are the on-call engineer for one node of an inference fleet. The node is a separate process that "
        "proxies /v1/chat/completions to an upstream model API using the settings in its config.yaml. "
        "It is unhealthy. Find out why and fix it.\n\n"
        "Working rules:\n"
        "- Gather evidence before you change anything: node_status, tail_log and read_config first.\n"
        "- Compare config.yaml with the standard configuration below. Every difference is a suspect.\n"
        "- Prefer the root cause over a restart. A restart alone fixes only a stuck process; if the cause is in "
        "the config, the files or the port, the fault returns.\n"
        "- Make the smallest change the evidence supports and keep every other setting as it is.\n"
        "- After a change, restart or start the node if needed, then probe_once to check it.\n"
        "- Call claim_fixed only when you can state the root cause. An independent health check runs on every claim "
        "(a probe, a wait, a burst of requests, a second probe). If it fails you get the evidence back.\n"
        "- You have a limited number of steps. Do not repeat a call that already told you what it can.\n"
        "- All paths are relative to the node directory (config.yaml, logs/node.log, data/).\n\n"
        "A healthy node in this fleet is configured like this:\n"
        f"{standard_config()}\n\n"
        f"{tool_schema()}\n\n"
        'Reply with exactly one JSON object per turn and nothing else: {"thought": "<one short sentence>", '
        '"action": "<tool name>", "args": {<arguments>}}. For example: '
        '{"thought": "Check why the node is failing.", "action": "tail_log", "args": {"n": 40}}'
    )


def render_card(row: dict[str, Any]) -> str:
    card = row.get("card") or {}
    fix = card.get("fix_steps") or card.get("fix") or []
    if isinstance(fix, str):
        fix = [fix]
    lines = [f"## {row['fault_class']} (v{row['version']})"]
    if card.get("signature"):
        lines.append(f"Signature: {card['signature']}")
    if card.get("diagnosis"):
        lines.append(f"Diagnosis: {card['diagnosis']}")
    if fix:
        lines.append("Fix: " + " ".join(f"{i}. {step}" for i, step in enumerate(fix, 1)))
    if card.get("pitfalls"):
        pitfalls = card["pitfalls"]
        lines.append("Pitfalls: " + (pitfalls if isinstance(pitfalls, str) else " ".join(pitfalls)))
    return "\n".join(lines)


def skill_block(skills: list[dict[str, Any]]) -> str:
    """Deterministic for a given set of cards: sorted by class, no timestamps."""
    if not skills:
        return "SKILL LIBRARY\nNo cards yet."
    cards = [render_card(row) for row in sorted(skills, key=lambda r: r["fault_class"])]
    return "SKILL LIBRARY (one card per fault class, from verified fixes)\n\n" + "\n\n".join(cards)


def incident_context(
    incident: dict[str, Any], hint: tuple[str, float] | None = None, handoff: str | None = None
) -> str:
    lines = [
        f"Incident {incident['id']} on node {incident['node_id']} (severity {incident['severity']}).",
        f"Symptom: {incident['symptom']}",
    ]
    if hint and hint[0] != "unknown":
        lines.append(f"Triage guess: {hint[0]} (confidence {hint[1]:.2f}). It is only a hint; check the evidence.")
    if handoff:
        lines.append("")
        lines.append(handoff)
    lines.append("")
    lines.append("Start with your first action.")
    return "\n".join(lines)


def build_messages(skills: list[dict[str, Any]], context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt()},
        {"role": "system", "content": skill_block(skills)},
        {"role": "user", "content": context},
    ]


@dataclass
class Turn:
    thought: str
    action: str
    args: dict[str, Any] = field(default_factory=dict)

    def canonical(self) -> str:
        return json.dumps({"thought": self.thought, "action": self.action, "args": self.args}, separators=(",", ":"))


def _as_args(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def parse_turn(text: str | None) -> Turn | None:
    obj = parse_action(text)
    if obj is None:
        return None
    action = obj.get("action") or obj.get("tool") or obj.get("name")
    if not isinstance(action, str) or not action.strip():
        return None
    args = _as_args(obj.get("args", obj.get("arguments", obj.get("parameters"))))
    if args is None:
        return None
    if not args:
        # Some replies put the arguments next to "action" instead of under "args".
        args = {k: v for k, v in obj.items() if k not in ("thought", "action", "tool", "name", "args", "arguments", "parameters")}
    thought = obj.get("thought")
    return Turn(str(thought).strip() if thought else "", action.strip(), args)


def clip(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters]"


def tool_result_message(action: str, ok: bool, text: str) -> dict[str, str]:
    return {"role": "user", "content": f"Result of {action} ({'ok' if ok else 'error'}):\n{clip(text)}"}


def rejection_message(details: str) -> dict[str, str]:
    return {"role": "user", "content": f"Health check failed: {details}. The node is not healthy. Continue."}
