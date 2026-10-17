"""Seeded fault injection and ground-truth storage.

Each injector makes the change an operator mistake would make: edit the config and restart, kill the process,
start something on the port, chmod a file, fill the disk. Ground truth goes to faults_truth only, never into
the node directory.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import random
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
import yaml

from config import settings
from understudy.db import Database
from understudy.fleet.nodeconfig import (
    ConfigError,
    canonical_config,
    dump_config,
    load_config_dict,
    parse_config_text,
)
from understudy.fleet.supervisor import Supervisor

TRAIN_CLASSES: tuple[str, ...] = settings.TRAINING_FAULT_CLASSES
HELDOUT_CLASSES: tuple[str, ...] = settings.HELDOUT_FAULT_CLASSES
ALL_CLASSES: tuple[str, ...] = TRAIN_CLASSES + HELDOUT_CLASSES
CLASS_NUMBERS: dict[str, int] = {name: i for i, name in enumerate(ALL_CLASSES, start=1)}
TRAIN_SEEDS = settings.TRAINING_SEEDS
HELDOUT_SEED_START = settings.HELDOUT_SEED_START

# Hostnames ending in .invalid never resolve, so the node's key is not sent to a host we do not control.
# The two real-host entries are wrong paths on the correct host and answer 404.
STALE_ENDPOINTS = (
    "https://api.inference.crusoecloud.com/openai/v1",
    "https://api.inference.crusoecloud.com/api/v1",
    "https://managed-inference-api-proxy.crusoecloud.invalid/v1",
    "https://api.crusoe.invalid/v1",
)
# Parameters the Crusoe gateway rejects with 403 "parameter ... is not allowed".
FORBIDDEN_PARAMS: tuple[tuple[str, Any], ...] = (
    ("enable_thinking", True),
    ("thinking_budget", 1024),
    ("reasoning_budget", 512),
    ("max_thinking_tokens", 512),
    ("thinking_mode", "on"),
    ("enable_reasoning", True),
)
WRONG_KEY_ENVS = (
    "CRUSOE_KEY",
    "CRUSOE_INFERENCE_KEY",
    "CRUSOE_API_TOKEN",
    "CRUSOE_APIKEY",
    "INFERENCE_API_KEY",
    "crusoe_api_key",
)
SPOOL_PREFIXES = ("spool", "core", "dump", "trace")


class FaultError(RuntimeError):
    pass


@dataclass
class Fault:
    node_id: str
    fault_class: str
    seed: int
    params: dict[str, Any]
    injected_at: float = field(default_factory=time.time)
    truth_id: int | None = None

    @property
    def number(self) -> int:
        return CLASS_NUMBERS[self.fault_class]


def seeded_rng(*parts: object) -> random.Random:
    """Hashing first keeps neighbouring seeds from producing correlated first draws."""
    digest = hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


def seed_kind(seed: int) -> str:
    if seed in TRAIN_SEEDS:
        return "train"
    if seed >= HELDOUT_SEED_START:
        return "heldout"
    return "other"


def random_seed(kind: str, rng: random.Random | None = None) -> int:
    rng = rng or random
    if kind == "heldout":
        return rng.randrange(HELDOUT_SEED_START, HELDOUT_SEED_START + 100_000)
    return rng.randrange(TRAIN_SEEDS.start, TRAIN_SEEDS.stop)


@dataclass
class Ctx:
    sup: Supervisor
    node_id: str
    rng: random.Random

    @property
    def paths(self):
        return self.sup.paths(self.node_id)

    def config(self) -> dict[str, Any]:
        try:
            return load_config_dict(self.paths.config)
        except (ConfigError, OSError):
            spec = self.sup.specs[self.node_id]
            return canonical_config(spec.id, spec.port, self.sup.model)

    def write_config(self, cfg: dict[str, Any]) -> None:
        self.paths.config.write_text(dump_config(cfg))

    async def edit_and_restart(self, mutate: Callable[[dict[str, Any]], dict[str, Any] | None]) -> dict[str, Any]:
        cfg = self.config()
        params = mutate(cfg) or {}
        self.write_config(cfg)
        await self.sup.restart(self.node_id)
        return params


async def _model_id_case(ctx: Ctx) -> dict[str, Any]:
    def mutate(cfg: dict[str, Any]) -> dict[str, Any]:
        original = cfg["model"]
        vendor, _, name = original.partition("/")
        options = {
            "lowercase": original.lower(),
            "uppercase": original.upper(),
            "vendor_uppercase": f"{vendor.upper()}/{name}",
            "vendor_style_prefix": original.replace("NVIDIA-", "nvidia-", 1),
            "name_lowercase": f"{vendor}/{name.lower()}",
        }
        options = {k: v for k, v in options.items() if v != original}
        kind = ctx.rng.choice(sorted(options))
        cfg["model"] = options[kind]
        return {"original": original, "wrong": options[kind], "variant": kind}

    return await ctx.edit_and_restart(mutate)


async def _stale_endpoint(ctx: Ctx) -> dict[str, Any]:
    def mutate(cfg: dict[str, Any]) -> dict[str, Any]:
        original = cfg["upstream_base_url"]
        wrong = ctx.rng.choice(STALE_ENDPOINTS)
        cfg["upstream_base_url"] = wrong
        return {"original": original, "wrong": wrong}

    return await ctx.edit_and_restart(mutate)


async def _starved_tokens(ctx: Ctx) -> dict[str, Any]:
    def mutate(cfg: dict[str, Any]) -> dict[str, Any]:
        original = cfg.get("max_tokens")
        # Above ~48 tokens the model finishes its reasoning and answers, so the node would not be faulty.
        cfg["max_tokens"] = ctx.rng.randint(8, 40)
        cfg["extra_body"] = {"chat_template_kwargs": {"enable_thinking": True}}
        return {"original_max_tokens": original, "max_tokens": cfg["max_tokens"]}

    return await ctx.edit_and_restart(mutate)


async def _forbidden_param(ctx: Ctx) -> dict[str, Any]:
    def mutate(cfg: dict[str, Any]) -> dict[str, Any]:
        key, value = ctx.rng.choice(FORBIDDEN_PARAMS)
        extra = dict(cfg.get("extra_body") or {})
        extra[key] = value
        cfg["extra_body"] = extra
        return {"param": key, "value": value}

    return await ctx.edit_and_restart(mutate)


async def _bad_key_env(ctx: Ctx) -> dict[str, Any]:
    def mutate(cfg: dict[str, Any]) -> dict[str, Any]:
        choices = [name for name in WRONG_KEY_ENVS if name not in os.environ and name != cfg.get("api_key_env")]
        cfg["api_key_env"] = ctx.rng.choice(choices)
        return {"original": settings.API_KEY_ENV, "wrong": cfg["api_key_env"]}

    return await ctx.edit_and_restart(mutate)


async def _port_squatter(ctx: Ctx) -> dict[str, Any]:
    await ctx.sup.stop(ctx.node_id)
    pid = await ctx.sup.spawn_squatter(ctx.node_id)
    await ctx.sup.start(ctx.node_id)
    return {"squatter_pid": pid, "port": ctx.sup.port_of(ctx.node_id)}


async def _disk_full(ctx: Ctx) -> dict[str, Any]:
    cfg = ctx.config()
    quota = int(cfg.get("quota_mb", settings.NODE_QUOTA_MB)) * 1024 * 1024
    data = ctx.paths.data
    data.mkdir(parents=True, exist_ok=True)
    prefix = ctx.rng.choice(SPOOL_PREFIXES)
    target = int(quota * ctx.rng.uniform(1.1, 1.4))
    count = ctx.rng.randint(6, 12)
    existing = sum(p.stat().st_size for p in data.rglob("*") if p.is_file())
    each = max(1, (target - existing) // count)
    stamp = int(time.time())
    for i in range(count):
        (data / f"{prefix}-{stamp}-{i:03d}.bin").write_bytes(b"\0" * each)
    await _wait_for(lambda: _quota_over(ctx), timeout=5.0)
    return {"prefix": prefix, "files": count, "bytes_each": each, "quota_bytes": quota}


async def _quota_over(ctx: Ctx) -> bool:
    status = ctx.sup.status(ctx.node_id)
    if status.state != "running":
        return True
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            reply = await client.get(f"http://127.0.0.1:{status.port}/local/stats")
        return bool(reply.json()["quota"]["over"])
    except (httpx.HTTPError, ValueError, KeyError):
        return False


async def _wait_for(check: Callable[[], Awaitable[bool]], timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return True
        await asyncio.sleep(interval)
    return False


async def _crashed_worker(ctx: Ctx) -> dict[str, Any]:
    status = ctx.sup.status(ctx.node_id)
    was_running = status.state == "running"
    await ctx.sup.kill(ctx.node_id, signal.SIGKILL)
    return {"signal": int(signal.SIGKILL), "was_running": was_running}


def _break_yaml(text: str, rng: random.Random) -> tuple[str, str]:
    """Applies one plausible hand-editing slip to the config text and returns (text, kind)."""

    def unclosed_bracket(t: str) -> str:
        return t.replace("max_tokens:", "max_tokens: [", 1) if "max_tokens:" in t else t

    def unterminated_quote(t: str) -> str:
        lines = t.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("model:"):
                lines[i] = f'model: "{line.split(":", 1)[1].strip()}'
        return "\n".join(lines) + "\n"

    def tab_indent(t: str) -> str:
        return t.replace("\n  ", "\n\t", 1)

    def stray_mapping(t: str) -> str:
        return t.replace("retry_backoff: true", "retry_backoff: true: false", 1)

    def unbalanced_brace(t: str) -> str:
        return t.replace("quota_mb:", "quota_mb: {", 1)

    options = {
        "unclosed_bracket": unclosed_bracket,
        "unterminated_quote": unterminated_quote,
        "tab_indent": tab_indent,
        "stray_mapping": stray_mapping,
        "unbalanced_brace": unbalanced_brace,
    }
    names = sorted(options)
    rng.shuffle(names)
    for name in names:
        broken = options[name](text)
        try:
            yaml.safe_load(broken)
        except yaml.YAMLError:
            return broken, name
    raise FaultError("could not produce a config that fails to parse")


async def _config_syntax(ctx: Ctx) -> dict[str, Any]:
    text = dump_config(ctx.config())
    broken, kind = _break_yaml(text, ctx.rng)
    try:
        parse_config_text(broken)
    except ConfigError as exc:
        line = exc.line
    else:
        raise FaultError("broken config still parses")
    ctx.paths.config.write_text(broken)
    await ctx.sup.restart(ctx.node_id)
    return {"variant": kind, "line": line}


async def _leaky_worker(ctx: Ctx) -> dict[str, Any]:
    def mutate(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["worker_recycle_after"] = ctx.rng.randint(2, 4)
        return {"worker_recycle_after": cfg["worker_recycle_after"]}

    return await ctx.edit_and_restart(mutate)


async def _log_permission(ctx: Ctx) -> dict[str, Any]:
    await ctx.sup.stop(ctx.node_id)
    log = ctx.paths.log
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    os.chmod(log, 0o000)
    await ctx.sup.start(ctx.node_id)
    return {"path": "logs/node.log", "mode": "000"}


async def _upstream_storm(ctx: Ctx) -> dict[str, Any]:
    state: dict[str, Any] = {}

    def mutate(cfg: dict[str, Any]) -> dict[str, Any]:
        limit = int(cfg.get("upstream_limit", settings.NODE_UPSTREAM_LIMIT))
        cfg["worker_concurrency"] = limit + ctx.rng.randint(4, 12)
        cfg["retry_backoff"] = False
        state["workers"] = cfg["worker_concurrency"]
        return {"worker_concurrency": cfg["worker_concurrency"], "upstream_limit": limit, "retry_backoff": False}

    params = await ctx.edit_and_restart(mutate)
    # A restarted node replays its queued requests at once; that burst is what trips the upstream limit.
    port = ctx.sup.port_of(ctx.node_id)
    body = {"messages": [{"role": "user", "content": "Reply with exactly: OK"}]}
    async with httpx.AsyncClient(timeout=30.0) as client:
        await asyncio.gather(
            *(client.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=body) for _ in range(state["workers"])),
            return_exceptions=True,
        )
    return params


INJECTORS: dict[str, Callable[[Ctx], Awaitable[dict[str, Any]]]] = {
    "model_id_case": _model_id_case,
    "stale_endpoint": _stale_endpoint,
    "starved_tokens": _starved_tokens,
    "forbidden_param": _forbidden_param,
    "bad_key_env": _bad_key_env,
    "port_squatter": _port_squatter,
    "disk_full": _disk_full,
    "crashed_worker": _crashed_worker,
    "config_syntax": _config_syntax,
    "leaky_worker": _leaky_worker,
    "log_permission": _log_permission,
    "upstream_storm": _upstream_storm,
}
assert set(INJECTORS) == set(ALL_CLASSES)


def resolve_class(name_or_number: str | int) -> str:
    if isinstance(name_or_number, int) or str(name_or_number).isdigit():
        number = int(name_or_number)
        for name, index in CLASS_NUMBERS.items():
            if index == number:
                return name
        raise FaultError(f"no fault class number {number}")
    if name_or_number not in INJECTORS:
        raise FaultError(f"unknown fault class {name_or_number!r}; choose from {', '.join(ALL_CLASSES)}")
    return str(name_or_number)


async def inject(
    sup: Supervisor, node_id: str, fault_class: str | int, seed: int | None = None, db: Database | None = None
) -> Fault:
    fault_class = resolve_class(fault_class)
    if node_id not in sup.specs:
        raise FaultError(f"unknown node {node_id!r}")
    if seed is None:
        seed = random_seed("train")
    ctx = Ctx(sup, node_id, seeded_rng(fault_class, seed))
    params = await INJECTORS[fault_class](ctx)
    fault = Fault(node_id, fault_class, seed, params)
    if db is not None:
        fault.truth_id = await db.add_fault_truth(node_id, fault_class, seed, params, zone=sup.specs[node_id].zone)
    return fault


async def storm(
    sup: Supervisor,
    node_ids: list[str],
    seed: int,
    db: Database | None = None,
    heldout: bool = False,
) -> list[Fault]:
    """Injects a different mix of faults on each node at once. heldout=True draws from all 12 classes and
    from the held-out seed range."""
    rng = seeded_rng("storm", seed)
    pool = list(ALL_CLASSES if heldout else TRAIN_CLASSES)
    picks = rng.sample(pool, len(node_ids)) if len(node_ids) <= len(pool) else [rng.choice(pool) for _ in node_ids]
    kind = "heldout" if heldout else "train"
    jobs = [inject(sup, node, cls, random_seed(kind, rng), db) for node, cls in zip(node_ids, picks)]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        raise FaultError(f"{len(errors)} of {len(jobs)} injections failed: {errors[0]!r}")
    return list(results)  # type: ignore[arg-type]


async def pick_storm_nodes(
    sup: Supervisor,
    seed: int,
    rows: list[int] | int | None = None,
    count: int | None = None,
    db: Database | None = None,
) -> list[str]:
    """Chooses 3-6 live nodes for a storm. rows limits the candidates to those rack rows; an int picks that
    many random rows. Nodes that are down or already carry an uncleared fault are skipped."""
    rng = seeded_rng("storm-nodes", seed)
    faulty = {row["node_id"] for row in await db.active_faults()} if db is not None else set()
    live = [
        s for s in sup.specs.values()
        if s.zone == "live" and s.id not in faulty and sup.status(s.id).state == "running"
    ]
    if isinstance(rows, int):
        all_rows = sorted({s.row for s in live})
        rows = rng.sample(all_rows, min(rows, len(all_rows)))
    if rows:
        live = [s for s in live if s.row in rows]
    count = count if count is not None else rng.randint(3, 6)
    count = max(1, min(count, len(live)))
    return [s.id for s in rng.sample(live, count)]
