"""Turns natural-language commands into fault injections, and drives the autopilot fault schedule."""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Collection, Iterable

from config import settings
from understudy import llm
from understudy.context import Context
from understudy.fleet import faults

log = logging.getLogger("understudy.chaos")

MAX_DOWN = 6
MAX_TARGETS = 6
FAULT_INTERVAL_S = (20.0, 40.0)
STORM_INTERVAL_S = 300.0
FIRST_STORM_S = 150.0
SWEEP_INTERVAL_S = 90.0
# A fault younger than this may not have shown up in the monitor yet, so a passing probe proves nothing.
SWEEP_MIN_AGE_S = 120.0
CONFIRMATION_WORDS = 18
ACTIONS = ("storm", "inject", "random")
ROWS = (1, 2, 3)

CLASS_DESCRIPTIONS = {
    "model_id_case": "the model id has the wrong letter case (break the model id)",
    "stale_endpoint": "the upstream URL points at a stale or wrong host (stale endpoint, bad hostname)",
    "starved_tokens": "max_tokens is far too low so replies come back empty (starve the tokens)",
    "forbidden_param": "the request carries a parameter the gateway forbids (forbidden parameter)",
    "bad_key_env": "the API key env var name is wrong so auth fails (bad or forgotten API key)",
    "port_squatter": "another process holds the node's port (squat the port)",
    "disk_full": "the data disk is filled past its quota (fill the disk)",
    "crashed_worker": "the node process is killed (kill the worker, crash it)",
    "config_syntax": "the config file has a YAML syntax error (break the config)",
    "leaky_worker": "the worker exits after a few requests (leak, flaky worker)",
    "log_permission": "the log file permissions are removed so the node cannot start (log permission)",
    "upstream_storm": "requests overrun the upstream limit and get a 429 storm (upstream storm)",
}

# Phrases that name a class in a command. Order matters only for words that appear inside another phrase.
CLASS_PHRASES: dict[str, tuple[str, ...]] = {
    "upstream_storm": ("upstream storm", "upstream_storm", "429"),
    "log_permission": ("log permission", "log_permission", "permission", "chmod"),
    "model_id_case": ("model id", "model_id", "model name", "model case"),
    "stale_endpoint": ("stale endpoint", "stale_endpoint", "endpoint", "hostname", "stale host"),
    "starved_tokens": ("starved token", "starved_token", "starve", "max tokens", "max_tokens", "token starv"),
    "forbidden_param": ("forbidden param", "forbidden_param", "forbidden", "bad param"),
    "bad_key_env": ("bad_key_env", "api key", "bad key", "key env", "credential"),
    "port_squatter": ("port_squatter", "squat", "port"),
    "disk_full": ("disk_full", "disk", "quota", "storage"),
    "crashed_worker": ("crashed_worker", "crash", "kill", "segfault"),
    "config_syntax": ("config_syntax", "config", "yaml", "syntax"),
    "leaky_worker": ("leaky_worker", "leak", "flaky"),
}
RANDOM_WORDS = re.compile(
    r"\b(surprise|random|randomly|something|anything|whatever|any node|any fault|any one|your choice|you pick|pick one|a fault)\b"
)
BREAK_WORDS = re.compile(r"\b(break|take down|knock|hit|fail|kill|crash|wreck|fault|storm|inject)\b")

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
ORDINAL_ROWS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
_NUMBER_RE = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b")
_ORDINAL_ROW_RE = re.compile(r"\b(first|1st|second|2nd|third|3rd)\s+row\b")
_SPOKEN_ZERO_RE = re.compile(r"(?<=\d)([\s-]+)(?:oh|zero)\b")
_NODE_RE = re.compile(r"\br\s*([1-3])[\s_-]*(?:0\s*)?([1-4])\b")
_RACK_RE = re.compile(r"\bracks?\s+r?([1-3])\s*[\s,_-]\s*(?:0\s*)?([1-4])\b")
_BARE_PAIR_RE = re.compile(r"\b([1-3])-([1-4])\b")
_ROW_WORD_RE = re.compile(r"\brows?\b")
_ROWS_RE = re.compile(r"\brows?\s+(\d+(?:\s*(?:,|&|and|-|to|through|\s)\s*\d+)*)")
_REF_RE = re.compile(r"\b(?:r\d+|pg)-\d+\b")
_NODE_ID_RE = re.compile(r"\br[1-3]-0[1-4]\b")

PARSER_PROMPT = (
    "You turn an operator's chaos command into JSON for a test harness. Reply with one JSON object and nothing else:\n"
    '{"action": "storm" | "inject" | "random" | null, "rows": [1-3, ...] or null, '
    '"targets": [{"node_id": "<node id>" or null, "fault_class": "<class>" or null}]}\n'
    "Actions:\n"
    "- storm: many mixed faults at once, on all nodes or on the rows given. targets is empty.\n"
    "- inject: specific faults. One target per node or per fault. node_id is null when the operator names a fault "
    "but no node; fault_class is null when the operator names a node but no fault.\n"
    "- random: the operator wants one arbitrary fault (surprise me, break something). targets is empty.\n"
    "- null: the text is not a request to break anything. Use rows null and targets empty.\n"
    "Valid node ids: {nodes}. Nodes are r<row>-0<rack>; rows are 1 to 3. Never invent other ids.\n"
    "Fault classes:\n{classes}\n"
    "Use null for anything the operator did not specify. Do not add explanations.\n"
    "Examples:\n"
    'storm rows 2 and 3 -> {"action": "storm", "rows": [2, 3], "targets": []}\n'
    'break r1-02 model id -> {"action": "inject", "rows": null, "targets": '
    '[{"node_id": "r1-02", "fault_class": "model_id_case"}]}\n'
    'fill the disk -> {"action": "inject", "rows": null, "targets": [{"node_id": null, "fault_class": "disk_full"}]}\n'
    'take down r2-01 -> {"action": "inject", "rows": null, "targets": [{"node_id": "r2-01", "fault_class": null}]}\n'
    'break the config on row 2 -> {"action": "inject", "rows": [2], "targets": '
    '[{"node_id": null, "fault_class": "config_syntax"}]}\n'
    'take down row 3 -> {"action": "storm", "rows": [3], "targets": []}\n'
    'surprise me -> {"action": "random", "rows": null, "targets": []}\n'
    'break something -> {"action": "random", "rows": null, "targets": []}\n'
    'break anything on row 1 -> {"action": "random", "rows": [1], "targets": []}\n'
    'what is the weather -> {"action": null, "rows": null, "targets": []}'
)


def normalize(text: str) -> str:
    """Rewrites spoken phrasing into a form the keyword parser and the model can rely on: lower case, digits for
    number words, possessives dropped, and rack references turned into node ids ("rack three four" -> "r3-04")."""
    s = text.lower().replace("\u2019", "'")
    s = re.sub(r"'s\b", "", s)
    s = _ORDINAL_ROW_RE.sub(lambda m: f"row {ORDINAL_ROWS[m.group(1)]}", s)
    s = re.sub(r"\bdash\b", "-", s)
    s = _NUMBER_RE.sub(lambda m: str(NUMBER_WORDS[m.group(1)]), s)
    s = _SPOKEN_ZERO_RE.sub(r"\g<1>0", s)
    s = _NODE_RE.sub(lambda m: f"r{m.group(1)}-0{m.group(2)}", s)
    s = _RACK_RE.sub(lambda m: f"r{m.group(1)}-0{m.group(2)}", s)
    # "rows 2-3" is a range of rows, so a bare pair is only a rack reference when the command names no rows.
    if not _ROW_WORD_RE.search(s):
        s = _BARE_PAIR_RE.sub(lambda m: f"r{m.group(1)}-0{m.group(2)}", s)
    return " ".join(s.split())


def extract_rows(normalized: str) -> list[int]:
    """Row numbers named after "row"/"rows", ranges expanded, in order and without duplicates."""
    rows: list[int] = []
    for match in _ROWS_RE.finditer(normalized):
        tokens = re.findall(r"\d+|-|\bto\b|\bthrough\b", match.group(1))
        i = 0
        while i < len(tokens):
            if tokens[i].isdigit():
                if i + 2 < len(tokens) and tokens[i + 1] in ("-", "to", "through") and tokens[i + 2].isdigit():
                    low, high = int(tokens[i]), int(tokens[i + 2])
                    values = range(low, high + 1) if 0 <= high - low <= 12 else [low, high]
                    i += 3
                else:
                    values = [int(tokens[i])]
                    i += 1
                rows.extend(v for v in values if v not in rows)
            else:
                i += 1
    return rows


def unknown_refs(normalized: str, node_ids: Collection[str]) -> list[str]:
    """Node-looking references in the command that are not live nodes."""
    return [ref for ref in dict.fromkeys(_REF_RE.findall(normalized)) if ref not in node_ids]


@dataclass(frozen=True)
class ChaosTarget:
    node_id: str | None
    fault_class: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"node_id": self.node_id, "fault_class": self.fault_class}


@dataclass(frozen=True)
class ChaosPlan:
    action: str
    rows: tuple[int, ...] | None = None
    targets: tuple[ChaosTarget, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rows": list(self.rows) if self.rows else None,
            "targets": [t.as_dict() for t in self.targets],
        }


def _clean_rows(raw: Any) -> tuple[tuple[int, ...] | None, bool]:
    """Returns (valid rows or None, ok). ok is False when rows were given but none is a valid row."""
    if raw is None or raw == [] or raw == "":
        return None, True
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    rows: list[int] = []
    for item in items:
        try:
            number = float(str(item).strip())
        except ValueError:
            continue
        if number != number or number in (float("inf"), float("-inf")) or number != int(number):
            continue
        value = int(number)
        if value in ROWS and value not in rows:
            rows.append(value)
    return (tuple(sorted(rows)), True) if rows else (None, False)


def _clean_class(raw: Any) -> str | None:
    if raw is None or isinstance(raw, bool):
        return None
    name = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if name in ("", "null", "none"):
        return None
    try:
        return faults.resolve_class(name)
    except (faults.FaultError, ValueError):
        raise ValueError(name) from None


def validate_plan(raw: Any, node_ids: Collection[str]) -> ChaosPlan | None:
    """Turns untrusted model output into a plan, dropping what is not valid. None means nothing usable is left."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return None
    rows, rows_ok = _clean_rows(raw.get("rows"))
    if not rows_ok:
        return None
    if action in ("storm", "random"):
        return ChaosPlan(action, rows)
    entries = raw.get("targets")
    if isinstance(entries, dict):
        entries = [entries]
    targets: list[ChaosTarget] = []
    seen: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        node = entry.get("node_id")
        node = None if node is None or str(node).strip().lower() in ("", "null", "none") else str(node).strip().lower()
        if node is not None and (node not in node_ids or node in seen):
            continue
        try:
            fault_class = _clean_class(entry.get("fault_class"))
        except ValueError:
            continue
        if node is None and fault_class is None:
            continue
        if node is not None:
            seen.add(node)
        targets.append(ChaosTarget(node, fault_class))
        if len(targets) == MAX_TARGETS:
            break
    return ChaosPlan("inject", rows, tuple(targets)) if targets else None


def ground_plan(plan: ChaosPlan, normalized: str, node_ids: Collection[str]) -> ChaosPlan | None:
    """Checks a model plan against the command text. Normalization rewrites every node reference to its id, so a
    node the text does not contain was made up, and a node the plan leaves out was missed: both discard the plan.
    Rows are taken from the text when it names them and dropped when it never mentions a row."""
    named = {n for n in _NODE_ID_RE.findall(normalized) if n in node_ids}
    planned = {t.node_id for t in plan.targets if t.node_id}
    if plan.action == "storm":
        planned = set()
    if named != planned:
        return None
    text_rows, _ = _clean_rows(extract_rows(normalized))
    if text_rows:
        rows = text_rows
    elif _ROW_WORD_RE.search(normalized):
        rows = plan.rows
    else:
        rows = None
    return ChaosPlan(plan.action, rows, plan.targets)


def _mentioned_classes(normalized: str) -> list[str]:
    """Classes named in the text, in order of first mention. A phrase inside a longer matched phrase does not count."""
    found: list[tuple[int, str]] = []
    claimed: list[tuple[int, int]] = []
    for name, phrases in CLASS_PHRASES.items():
        best: tuple[int, int] | None = None
        for phrase in phrases:
            pattern = r"\b" + re.escape(phrase)
            for m in re.finditer(pattern, normalized):
                span = m.span()
                if any(a <= span[0] and span[1] <= b for a, b in claimed):
                    continue
                if best is None or span[0] < best[0]:
                    best = span
        if best is not None:
            claimed.append(best)
            found.append((best[0], name))
    return [name for _, name in sorted(found)]


def keyword_plan(normalized: str, node_ids: Collection[str]) -> ChaosPlan | None:
    """Deterministic parse of an already normalized command. None when it names nothing to do."""
    rows, _ = _clean_rows(extract_rows(normalized))
    nodes = [n for n in dict.fromkeys(_NODE_ID_RE.findall(normalized)) if n in node_ids][:MAX_TARGETS]
    classes = _mentioned_classes(normalized)
    without_classes = normalized.replace("upstream storm", " ")
    if nodes:
        if len(classes) == len(nodes):
            pairs = list(zip(nodes, classes))
        else:
            pairs = [(n, classes[0] if classes else None) for n in nodes]
        return ChaosPlan("inject", rows, tuple(ChaosTarget(n, c) for n, c in pairs))
    if re.search(r"\bstorm", without_classes) and not classes:
        return ChaosPlan("storm", rows)
    if classes:
        return ChaosPlan("inject", rows, tuple(ChaosTarget(None, c) for c in classes[:MAX_TARGETS]))
    if re.search(r"\bstorm", without_classes):
        return ChaosPlan("storm", rows)
    if RANDOM_WORDS.search(normalized):
        return ChaosPlan("random", rows)
    if rows and BREAK_WORDS.search(normalized):
        return ChaosPlan("storm", rows)
    if re.search(r"\b(everything|all rows|every row)\b", normalized) and BREAK_WORDS.search(normalized):
        return ChaosPlan("storm")
    return None


_UNITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")


def _spoken(n: int) -> str:
    return _UNITS[n] if 0 <= n < len(_UNITS) else str(n)


def _row_phrase(rows: Iterable[int]) -> str:
    words = [_spoken(r) for r in sorted(set(rows))]
    if len(words) == 1:
        return f"row {words[0]}"
    return "rows " + ", ".join(words[:-1]) + " and " + words[-1]


def _plural(n: int, noun: str) -> str:
    return f"{_spoken(n)} {noun}" + ("" if n == 1 else "s")


def confirmation_text(
    plan: ChaosPlan, injected: list[dict[str, Any]], skipped: list[dict[str, Any]], node_rows: dict[str, int]
) -> str:
    """One calm line built from what was actually injected."""
    if plan.action == "storm":
        rows = sorted({node_rows[i["node_id"]] for i in injected if i["node_id"] in node_rows})
        where = f" on {_row_phrase(rows)}" if rows else ""
        return f"Storm injected{where}: {_plural(len(injected), 'fault')}."
    skip_names = ", ".join(s.get("node_id") or "an unplaced fault" for s in skipped)
    tail = f" Skipped {skip_names}." if skipped else ""
    detail = ", ".join(f"{i['fault_class'].replace('_', ' ')} on {i['node_id']}" for i in injected)
    line = f"Injected {detail}.{tail}"
    if len(line.split()) > CONFIRMATION_WORDS:
        line = f"Injected {_plural(len(injected), 'fault')} on {', '.join(i['node_id'] for i in injected)}.{tail}"
    if len(line.split()) > CONFIRMATION_WORDS:
        line = f"Injected {_plural(len(injected), 'fault')}." + (f" Skipped {_spoken(len(skipped))}." if skipped else "")
    return line


class ChaosError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ChaosOps:
    def __init__(self, ctx: Context, rng: random.Random | None = None):
        self.ctx = ctx
        self.rng = rng or random.Random()
        self._busy: set[str] = set()
        self._tasks: list[asyncio.Task] = []

    # selection ------------------------------------------------------------------------

    async def _faulty_nodes(self) -> set[str]:
        return {row["node_id"] for row in await self.ctx.db.active_faults()}

    async def free_live_nodes(self, rows: list[int] | None = None) -> list[str]:
        faulty = await self._faulty_nodes()
        out = []
        for node_id, view in self.ctx.state.nodes.items():
            if view.spec.zone != "live" or view.status != "healthy":
                continue
            if node_id in faulty or node_id in self._busy or (rows and view.spec.row not in rows):
                continue
            if self.ctx.sup.status(node_id).state != "running":
                continue
            out.append(node_id)
        return out

    async def down_count(self) -> int:
        state = self.ctx.state
        unhealthy = {n for n, v in state.nodes.items() if v.spec.zone == "live" and v.status != "healthy"}
        faulty = {n for n in await self._faulty_nodes() if state.nodes[n].spec.zone == "live"}
        return len(unhealthy | faulty | self._busy)

    # injection ------------------------------------------------------------------------

    async def inject(self, node_id: str, fault_class: str, seed: int | None = None) -> dict[str, Any]:
        ctx = self.ctx
        if node_id not in ctx.sup.specs:
            raise ChaosError(404, f"unknown node {node_id}")
        try:
            fault_class = faults.resolve_class(fault_class)
        except faults.FaultError as exc:
            raise ChaosError(422, str(exc)) from None
        view = ctx.state.nodes[node_id]
        if view.spec.zone != "live" or view.status != "healthy" or node_id in self._busy or await ctx.db.active_faults(node_id):
            raise ChaosError(409, f"{node_id} is not a healthy live node")
        self._busy.add(node_id)
        try:
            kind = "heldout" if fault_class in settings.HELDOUT_FAULT_CLASSES else "train"
            fault = await faults.inject(ctx.sup, node_id, fault_class, seed or faults.random_seed(kind, self.rng), ctx.db)
        except faults.FaultError as exc:
            raise ChaosError(500, str(exc)) from None
        finally:
            self._busy.discard(node_id)
        log.info("injected %s on %s (seed %s)", fault_class, node_id, fault.seed)
        return {"node_id": node_id, "fault_class": fault_class}

    async def random_fault(self) -> dict[str, Any] | None:
        free = await self.free_live_nodes()
        if not free:
            return None
        node_id = self.rng.choice(free)
        fault_class = self.rng.choice(settings.TRAINING_FAULT_CLASSES)
        try:
            return await self.inject(node_id, fault_class)
        except ChaosError as exc:
            log.warning("random fault skipped: %s", exc.message)
            return None

    async def storm(self, rows: list[int] | int | None = None, count: int | None = None) -> list[dict[str, Any]]:
        ctx = self.ctx
        down = await self.down_count()
        room = MAX_DOWN - down
        if room <= 0:
            raise ChaosError(409, f"no room: {down} nodes are down or faulty")
        candidates = await self.free_live_nodes(rows if isinstance(rows, list) else None)
        if not candidates:
            where = f" in {_row_phrase(rows)}" if isinstance(rows, list) and rows else ""
            raise ChaosError(409, f"no free healthy nodes{where}")
        seed = faults.random_seed("train", self.rng)
        nodes = await faults.pick_storm_nodes(ctx.sup, seed, rows, count, ctx.db)
        nodes = [n for n in nodes if ctx.state.status(n) == "healthy" and n not in self._busy][:room]
        if not nodes:
            where = f" in {_row_phrase(rows)}" if isinstance(rows, list) and rows else ""
            raise ChaosError(409, f"no free healthy nodes{where}")
        self._busy.update(nodes)
        try:
            planted = await faults.storm(ctx.sup, nodes, seed, ctx.db)
        except faults.FaultError as exc:
            raise ChaosError(500, str(exc)) from None
        finally:
            self._busy.difference_update(nodes)
        log.info("storm on %s", ", ".join(nodes))
        return [{"node_id": f.node_id, "fault_class": f.fault_class} for f in planted]

    # natural language -----------------------------------------------------------------

    def _live_ids(self) -> set[str]:
        return {n for n, spec in self.ctx.sup.specs.items() if spec.zone == "live"}

    async def command(self, text: str) -> dict[str, Any]:
        ctx = self.ctx
        agent = ctx.spawn("chaos", "nano", ctx.model_id("nano"))
        try:
            plan = await self._plan(text, agent)
            injected, skipped = await self._execute(plan)
            node_rows = {n: spec.row for n, spec in ctx.sup.specs.items()}
            line = confirmation_text(plan, injected, skipped, node_rows)
            ctx.think(agent, line)
            try:
                await ctx.say(line, "high", polish=False)
            except Exception as exc:
                log.warning("chaos confirmation not narrated: %r", exc)
            body: dict[str, Any] = {"injected": injected, "plan": plan.as_dict(), "text": line}
            if skipped:
                body["skipped"] = skipped
            return body
        finally:
            ctx.done(agent)

    async def _plan(self, text: str, agent: str) -> ChaosPlan:
        normalized = normalize(text)
        live = self._live_ids()
        bad = unknown_refs(normalized, live)
        if bad:
            raise ChaosError(422, f"unknown node {', '.join(bad)}")
        missing = [r for r in extract_rows(normalized) if r not in ROWS]
        if missing:
            raise ChaosError(422, f"no such row {', '.join(map(str, missing))}; rows are 1 to 3")
        plan = await self._model_plan(normalized, live, agent) if normalized else None
        if plan is None:
            plan = keyword_plan(normalized, live)
        if plan is None:
            raise ChaosError(422, f"could not understand: {text.strip()}")
        return plan

    async def _model_plan(self, normalized: str, live: Collection[str], agent: str) -> ChaosPlan | None:
        classes = "\n".join(f"- {name}: {CLASS_DESCRIPTIONS.get(name, '')}" for name in faults.ALL_CLASSES)
        system = PARSER_PROMPT.replace("{nodes}", ", ".join(sorted(live))).replace("{classes}", classes)
        try:
            result = await self.ctx.chat(
                "chaos", "nano",
                [{"role": "system", "content": system}, {"role": "user", "content": normalized}],
                thinking=False, max_tokens=200, temperature=0.0, meta={"agent_id": agent},
            )
        except llm.BudgetExceeded:
            return None
        except Exception as exc:
            log.warning("chaos parser fell back to keywords: %r", exc)
            return None
        plan = validate_plan(llm.parse_action(result.content), live)
        return ground_plan(plan, normalized, live) if plan else None

    async def _free_node(self, rows: tuple[int, ...] | None) -> str:
        free = await self.free_live_nodes(list(rows) if rows else None)
        if not free:
            where = f" in {_row_phrase(rows)}" if rows else ""
            raise ChaosError(409, f"no free healthy nodes{where}")
        return self.rng.choice(free)

    async def _execute(self, plan: ChaosPlan) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if plan.action == "storm":
            return await self.storm(list(plan.rows) if plan.rows else None), []
        targets = plan.targets or (ChaosTarget(None, None),)
        injected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        statuses: list[int] = []
        for target in targets:
            try:
                down = await self.down_count()
                if down >= MAX_DOWN:
                    raise ChaosError(409, f"no room: {down} nodes are down or faulty")
                node = target.node_id or await self._free_node(plan.rows)
                fault_class = target.fault_class or self.rng.choice(settings.TRAINING_FAULT_CLASSES)
                injected.append(await self.inject(node, fault_class))
            except ChaosError as exc:
                log.warning("command skipped %s: %s", target.node_id or "a free node", exc.message)
                statuses.append(exc.status)
                skipped.append({"node_id": target.node_id, "fault_class": target.fault_class, "reason": exc.message})
        if not injected:
            raise ChaosError(409 if 409 in statuses else statuses[0], skipped[0]["reason"])
        return injected, skipped

    # autopilot ------------------------------------------------------------------------

    async def _fault_loop(self) -> None:
        while True:
            await asyncio.sleep(self.rng.uniform(*FAULT_INTERVAL_S))
            if await self._held_back():
                continue
            try:
                await self.random_fault()
            except Exception:
                log.exception("scheduled fault failed")

    async def _storm_loop(self) -> None:
        delay = FIRST_STORM_S
        while True:
            await asyncio.sleep(delay)
            delay = STORM_INTERVAL_S * self.rng.uniform(0.9, 1.1)
            if await self._held_back():
                continue
            try:
                await self.storm(rows=1)
            except ChaosError as exc:
                log.info("scheduled storm skipped: %s", exc.message)
            except Exception:
                log.exception("scheduled storm failed")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_S * self.rng.uniform(0.9, 1.1))
            try:
                await self.sweep_stale_faults()
            except Exception:
                log.exception("fault sweep failed")

    async def sweep_stale_faults(self) -> str | None:
        """Clears the fault row of one node that is healthy, has no open incident and passes the referee.

        An incident that ends unresolved leaves its fault row uncleared even when the node later recovers, and
        uncleared rows count toward MAX_DOWN. The referee decides, so a node that still fails keeps its row."""
        ctx = self.ctx
        now = time.time()
        open_nodes = {row["node_id"] for row in await ctx.db.open_incidents()}
        for row in await ctx.db.active_faults():
            node = row["node_id"]
            view = ctx.state.nodes.get(node)
            if (
                view is None or view.spec.zone != "live" or view.status != "healthy"
                or node in open_nodes or node in self._busy or now - row["injected_at"] < SWEEP_MIN_AGE_S
            ):
                continue
            self._busy.add(node)
            try:
                verdict = await ctx.referee.verify(node)
                # The monitor or a responder may have taken the node while the referee was probing it.
                open_now = {r["node_id"] for r in await ctx.db.open_incidents()}
                if verdict.verdict != "verified" or ctx.state.status(node) != "healthy" or node in open_now:
                    log.info("fault sweep left %s alone: %s", node, verdict.note)
                    return None
                await ctx.db.clear_fault_truth(node)
                if ctx.monitor is not None:
                    ctx.monitor.reset(node)
            finally:
                self._busy.discard(node)
            log.info("fault sweep cleared the stale fault row on %s", node)
            return node
        return None

    async def _held_back(self) -> bool:
        gate = self.ctx.gate
        if gate is not None and gate.paused:
            return True
        return await self.down_count() >= MAX_DOWN

    def start_autopilot(self) -> None:
        self._tasks = [
            asyncio.create_task(self._fault_loop()),
            asyncio.create_task(self._storm_loop()),
            asyncio.create_task(self._sweep_loop()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
