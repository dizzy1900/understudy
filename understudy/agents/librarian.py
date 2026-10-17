"""Maintains the skill library and compiles proven classes into fix scripts."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from config import settings
from understudy import llm
from understudy.agents import protocol
from understudy.agents.script_child import ScriptRejected, check_source
from understudy.agents.verifier import render_trajectory
from understudy.context import Context
from understudy.events import SkillUpdated

log = logging.getLogger("understudy.librarian")

COOLDOWN_S = 60.0
CARD_LIMIT = 900
TRAJECTORIES = 3
MIN_QUALITY = 4
COMPILE_MIN_FIXES = 5
MAX_COMPILE_ATTEMPTS = 3
CARD_TOKENS = 4096
SCRIPT_TOKENS = 6144

CARD_PROMPT = (
    "You keep a short runbook card for one fault class of an inference node fleet. Engineers read the card before "
    "they work an incident, so it must be exact and brief. You get the current card, the last verified fixes of "
    "this class as tool-call trajectories, and the results of held-out trials when there are any.\n"
    "Rewrite the card as one JSON object with these keys only:\n"
    '  "signature": what the symptom looks like (status codes, error text, node_status and log lines), at most 150 characters\n'
    '  "diagnosis": how to confirm the cause, at most 200 characters\n'
    '  "fix": a list of at most 5 short steps that name the tools to call (for example write_config, restart_node), at most 330 characters in total\n'
    '  "pitfalls": what looks like a fix but is not, or what breaks the fix, at most 150 characters\n'
    '  "change": one sentence on what changed against the current card, at most 100 characters\n'
    "Rules: keep what the trajectories and held-out passes support; drop anything they contradict; when held-out "
    "results disagree with a step, trust the held-out results. Do not invent facts that are in none of the inputs. "
    "Plain text, no markdown. Reply with the JSON object only."
)

SCRIPT_PROMPT = (
    "Write a Python fix script for one fault class of an inference node fleet. It must define one function, "
    "fix(tools), that repairs a node showing this fault and returns (summary, root_cause) as two strings.\n"
    "Rules:\n"
    "- Import only re, json and yaml. No other modules, no classes, no open(), eval(), exec() or getattr(), no "
    "attributes starting with an underscore, no yaml.load (use yaml.safe_load).\n"
    "- Reach the node only through tools. Every call returns an object with .ok (bool) and .text (str):\n"
    "  tools.node_status(), tools.tail_log(n=40), tools.read_config(), tools.read_config(rel='exit.json'), "
    "tools.write_config(yaml_text=...), tools.list_dir(rel='.'), tools.delete_files(glob='*.bin'), "
    "tools.fix_permissions(rel='logs/node.log'), tools.restart_node(), tools.stop_node(), tools.start_node(), "
    "tools.port_owner(), tools.kill_squatter(pid=123), tools.set_port(port=9201), tools.list_models(), tools.probe_once().\n"
    "- The fault comes in variants (different wrong values, different file names), so read the current state first, "
    "decide from it, and change only what is wrong. Do not hard-code the broken value.\n"
    "- Edit config with yaml.safe_load and yaml.safe_dump on the text from read_config. A healthy config is:\n"
    f"{protocol.standard_config()}\n"
    "- Finish by restarting or starting the node if needed and calling tools.probe_once(). Raise an Exception with a "
    "clear message when the state is not the one this script handles.\n"
    "- The script has 30 seconds in total.\n"
    "Reply with the Python source only, in one ```python block."
)

_CODE_FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.S)


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _normalise(card: dict[str, Any]) -> tuple[Any, ...]:
    fix = card.get("fix_steps") or []
    return (
        " ".join(str(card.get("signature", "")).lower().split()),
        " ".join(str(card.get("diagnosis", "")).lower().split()),
        tuple(" ".join(str(step).lower().split()) for step in fix),
        " ".join(str(card.get("pitfalls", "")).lower().split()),
    )


def render_length(fault_class: str, card: dict[str, Any]) -> int:
    return len(protocol.render_card({"fault_class": fault_class, "card": card}))


def fit_card(fault_class: str, card: dict[str, Any]) -> dict[str, Any]:
    """Trims the longest field until the rendered card is within CARD_LIMIT characters."""
    card = dict(card)
    card["fix_steps"] = list(card["fix_steps"])
    while render_length(fault_class, card) > CARD_LIMIT:
        over = render_length(fault_class, card) - CARD_LIMIT
        fields = {
            "pitfalls": len(card["pitfalls"]),
            "diagnosis": len(card["diagnosis"]),
            "signature": len(card["signature"]),
        }
        name = max(fields, key=lambda k: fields[k])
        if fields[name] > 40:
            card[name] = _clean(card[name], max(40, fields[name] - over))
        elif len(card["fix_steps"]) > 1:
            card["fix_steps"].pop()
        else:
            card[name] = _clean(card[name], max(10, fields[name] // 2))
            if all(v <= 10 for v in fields.values()):
                break
    return card


class Librarian:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiting: dict[str, asyncio.Task] = {}
        self._compiling: set[str] = set()

    # triggers -------------------------------------------------------------------------

    async def after_verified(self, fault_class: str, quality: int | None) -> None:
        """Called when an incident closes verified. At most one card update per class per COOLDOWN_S."""
        if quality is None or quality < MIN_QUALITY or fault_class not in settings.TRAINING_FAULT_CLASSES:
            return
        wait = self._last.get(fault_class, 0.0) + COOLDOWN_S - time.monotonic()
        if wait <= 0:
            await self._update_safely(fault_class)
        elif fault_class not in self._waiting:
            self._waiting[fault_class] = self.ctx.background(self._later(fault_class, wait))
        await self.maybe_compile(fault_class)

    async def _later(self, fault_class: str, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
        finally:
            self._waiting.pop(fault_class, None)
        await self._update_safely(fault_class)

    async def _update_safely(self, fault_class: str) -> None:
        try:
            await self.update_card(fault_class)
        except llm.BudgetExceeded:
            log.info("card update for %s skipped: budget cap reached", fault_class)
        except Exception:
            log.exception("card update for %s failed", fault_class)

    async def backfill(self) -> None:
        """Writes a first card for every class that has verified fixes but no card, for example after a restart
        with an empty library."""
        have = {row["fault_class"] for row in await self.ctx.db.latest_skills()}
        for fault_class in settings.TRAINING_FAULT_CLASSES:
            if fault_class in have:
                continue
            if await self._fixes(fault_class, 1):
                await self._update_safely(fault_class)

    async def stop(self) -> None:
        for task in list(self._waiting.values()):
            task.cancel()
        self._waiting.clear()

    # cards ----------------------------------------------------------------------------

    async def _fixes(self, fault_class: str, limit: int) -> list[dict[str, Any]]:
        return await self.ctx.db.fetchall(
            "SELECT * FROM incidents WHERE fault_class = ? AND zone = 'live' AND status = 'verified' AND quality >= ?"
            " ORDER BY closed_at DESC LIMIT ?",
            (fault_class, MIN_QUALITY, limit),
        )

    async def _evidence(self, fault_class: str) -> str:
        row = await self.ctx.db.fetchone(
            "SELECT COUNT(*) AS n, AVG(steps) AS steps FROM incidents"
            " WHERE fault_class = ? AND zone = 'live' AND status = 'verified'",
            (fault_class,),
        )
        n = int(row["n"]) if row else 0
        return f"{n} verified fixes, mean {float(row['steps'] or 0):.1f} steps"

    async def _heldout_notes(self, fault_class: str) -> str:
        db = self.ctx.db
        rows = await db.fetchall(
            "SELECT rung, SUM(passed) AS passed, COUNT(*) AS total FROM heldout_reports"
            " WHERE fault_class = ? AND kind = 'proving' GROUP BY rung",
            (fault_class,),
        )
        if not rows:
            return "No held-out trials yet."
        lines = [f"- {r['rung']} rung: {int(r['passed'])} of {r['total']} held-out instances fixed" for r in rows]
        failed = await db.fetchall(
            "SELECT rung, detail_json FROM heldout_reports WHERE fault_class = ? AND kind = 'proving' AND passed = 0"
            " ORDER BY id DESC LIMIT 3",
            (fault_class,),
        )
        for row in failed:
            reason = (json.loads(row["detail_json"]) if row["detail_json"] else {}).get("reason")
            if reason:
                lines.append(f"- a {row['rung']} trial failed: {_clean(reason, 160)}")
        return "\n".join(lines)

    async def _trajectory(self, incident: dict[str, Any]) -> str:
        actions = await self.ctx.db.actions_for(incident["id"])
        claims = [a for a in actions if a["tool"] == "claim_fixed" and a["ok"]]
        lines = [f"Incident symptom: {_clean(incident['symptom'], 160)}", render_trajectory(actions, 16)]
        if claims:
            claim = json.loads(claims[-1]["args_json"])
            lines.append(f"Root cause given: {_clean(claim.get('root_cause'), 200)}")
        return "\n".join(lines)

    async def update_card(self, fault_class: str) -> int | None:
        """Returns the new version, or None when the card did not change."""
        ctx = self.ctx
        lock = self._locks.setdefault(fault_class, asyncio.Lock())
        async with lock:
            self._last[fault_class] = time.monotonic()
            fixes = await self._fixes(fault_class, TRAJECTORIES)
            if not fixes:
                return None
            current = next((r for r in await ctx.db.latest_skills() if r["fault_class"] == fault_class), None)
            parts = [f"Fault class: {fault_class}", ""]
            parts.append("Current card:\n" + (protocol.render_card(current) if current else "(none yet)"))
            parts.append("\nHeld-out results for this class:\n" + await self._heldout_notes(fault_class))
            for i, incident in enumerate(fixes, 1):
                parts.append(f"\nVerified fix {i} of {len(fixes)}, newest first:\n" + await self._trajectory(incident))
            agent = ctx.spawn("librarian", "super", ctx.model_id("super"))
            try:
                ctx.think(agent, f"Updating the {fault_class} card from the last {len(fixes)} verified fixes.")
                raw = await self._ask_card(agent, "\n".join(parts))
                if raw is None:
                    ctx.think(agent, "The model did not return a usable card; keeping the current one.")
                    return None
                card = {
                    "signature": _clean(raw.get("signature"), 220),
                    "diagnosis": _clean(raw.get("diagnosis"), 260),
                    "fix_steps": [_clean(s, 140) for s in raw["fix"]][:5],
                    "pitfalls": _clean(raw.get("pitfalls"), 220),
                }
                card["evidence"] = await self._evidence(fault_class)
                card = fit_card(fault_class, card)
                if current and _normalise(card) == _normalise(current["card"]):
                    ctx.think(agent, f"The {fault_class} card already says this; no new version.")
                    return None
                change = _clean(raw.get("change"), 100) or "Rewritten from recent fixes."
                version = await ctx.db.save_skill(fault_class, card, change)
                ctx.bus.publish(SkillUpdated(fault_class=fault_class, version=version, summary=change))
                ctx.think(agent, f"{fault_class} card is now v{version}: {change}")
                log.info("skill card %s v%d: %s", fault_class, version, change)
                return version
            finally:
                ctx.done(agent)

    async def _ask_card(self, agent: str, user: str) -> dict[str, Any] | None:
        messages = [{"role": "system", "content": CARD_PROMPT}, {"role": "user", "content": user}]
        for attempt in range(2):
            result = await self.ctx.chat(
                "librarian", "super", messages, thinking=True, max_tokens=CARD_TOKENS, temperature=0.3,
                meta={"agent_id": agent},
            )
            parsed = llm.parse_action(result.content)
            if isinstance(parsed, dict) and isinstance(parsed.get("fix"), (list, str)) and parsed.get("signature"):
                if isinstance(parsed["fix"], str):
                    parsed["fix"] = [s for s in re.split(r"\s*\d+[.)]\s*", parsed["fix"]) if s.strip()] or [parsed["fix"]]
                return parsed
            messages = [*messages, {"role": "assistant", "content": result.content[:1500] or "(empty)"},
                        {"role": "user", "content": 'Reply with one JSON object with the keys "signature", "diagnosis", "fix", "pitfalls" and "change".'}]
        return None

    # scripts --------------------------------------------------------------------------

    def script_path(self, fault_class: str):
        return self.ctx.scripts_dir / f"{fault_class}.py"

    async def maybe_compile(self, fault_class: str) -> bool:
        """Compiles a script once a class sits at the lightning rung with enough verified lightning fixes."""
        ctx = self.ctx
        if fault_class in self._compiling or self.script_path(fault_class).exists():
            return False
        if (await ctx.db.get_ladder()).get(fault_class) != "lightning":
            return False
        fixes = int(
            await ctx.db.scalar(
                "SELECT COUNT(*) FROM incidents WHERE fault_class = ? AND zone = 'live' AND status = 'verified'"
                " AND rung = 'lightning'",
                (fault_class,),
            )
            or 0
        )
        if fixes < COMPILE_MIN_FIXES:
            return False
        try:
            return await self.compile_script(fault_class)
        except llm.BudgetExceeded:
            return False
        except Exception:
            log.exception("script compile for %s failed", fault_class)
            return False

    async def compile_script(self, fault_class: str, feedback: str | None = None) -> bool:
        ctx = self.ctx
        self._compiling.add(fault_class)
        agent = ctx.spawn("librarian", "super", ctx.model_id("super"))
        try:
            ctx.think(agent, f"Compiling a fix script for {fault_class} from verified fixes.")
            card = next((r for r in await ctx.db.latest_skills() if r["fault_class"] == fault_class), None)
            fixes = await self._fixes(fault_class, TRAJECTORIES)
            parts = [f"Fault class: {fault_class}", "", "Card:\n" + (protocol.render_card(card) if card else "(none)")]
            for i, incident in enumerate(fixes, 1):
                parts.append(f"\nVerified fix {i}:\n" + await self._trajectory(incident))
            if feedback:
                parts.append(f"\nThe previous version of the script failed on held-out instances:\n{_clean(feedback, 600)}")
            messages = [{"role": "system", "content": SCRIPT_PROMPT}, {"role": "user", "content": "\n".join(parts)}]
            for attempt in range(2):
                result = await ctx.chat(
                    "librarian", "super", messages, thinking=True, max_tokens=SCRIPT_TOKENS, temperature=0.2,
                    meta={"agent_id": agent},
                )
                source = self._extract_source(result.content)
                try:
                    check_source(source)
                except ScriptRejected as exc:
                    log.info("script for %s rejected: %s", fault_class, exc)
                    messages = [*messages, {"role": "assistant", "content": result.content[:3000] or "(empty)"},
                                {"role": "user", "content": f"The script was rejected: {exc}. Fix that and send the whole script again."}]
                    continue
                path = self.script_path(fault_class)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source.rstrip() + "\n", encoding="utf-8")
                ctx.think(agent, f"Wrote {path.name} ({len(source.splitlines())} lines).")
                await ctx.say(f"Compiled a fix script for {fault_class}. It still has to pass held-out trials before it is used.", "low", polish=False)
                return True
            ctx.think(agent, f"No acceptable script for {fault_class}.")
            return False
        finally:
            self._compiling.discard(fault_class)
            ctx.done(agent)

    @staticmethod
    def _extract_source(text: str) -> str:
        text = text or ""
        match = _CODE_FENCE.search(text)
        if match:
            return match.group(1).strip()
        return re.sub(r"^\s*```(?:python)?", "", text).strip()
