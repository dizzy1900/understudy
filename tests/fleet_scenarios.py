"""Inject, verify-fails, apply the known fix, verify-passes: one scenario per fault class.

The fixes here are written by hand, per class, and deliberately minimal. They show every injected fault can be
repaired through the same surface a responder gets (config edit, restart, delete files, chmod, kill squatter).
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import settings
from understudy.fleet import faults
from understudy.fleet.nodeconfig import canonical_config, dump_config, load_config_dict
from understudy.fleet.referee import Referee, Verdict
from understudy.fleet.supervisor import Supervisor

SPOOL_GLOBS = tuple(f"{prefix}-*.bin" for prefix in faults.SPOOL_PREFIXES)


@dataclass
class Env:
    sup: Supervisor
    referee: Referee
    upstream_url: str
    model: str


async def _edit(env: Env, node_id: str, mutate: Callable[[dict], None]) -> None:
    paths = env.sup.paths(node_id)
    cfg = load_config_dict(paths.config)
    mutate(cfg)
    paths.config.write_text(dump_config(cfg))
    await env.sup.restart(node_id)


async def fix_model_id_case(env: Env, node_id: str) -> None:
    await _edit(env, node_id, lambda cfg: cfg.update(model=env.model))


async def fix_stale_endpoint(env: Env, node_id: str) -> None:
    await _edit(env, node_id, lambda cfg: cfg.update(upstream_base_url=env.upstream_url))


async def fix_starved_tokens(env: Env, node_id: str) -> None:
    await _edit(
        env, node_id,
        lambda cfg: cfg.update(max_tokens=settings.NODE_MAX_TOKENS, extra_body={"chat_template_kwargs": {"enable_thinking": False}}),
    )


async def fix_forbidden_param(env: Env, node_id: str) -> None:
    def mutate(cfg: dict) -> None:
        cfg["extra_body"] = {k: v for k, v in cfg["extra_body"].items() if k == "chat_template_kwargs"}

    await _edit(env, node_id, mutate)


async def fix_bad_key_env(env: Env, node_id: str) -> None:
    await _edit(env, node_id, lambda cfg: cfg.update(api_key_env=settings.API_KEY_ENV))


async def fix_port_squatter(env: Env, node_id: str) -> None:
    port = env.sup.port_of(node_id)
    owner = env.sup.port_owner(port)
    assert owner is not None and owner["fleet_tracked"], owner
    assert await env.sup.kill_squatter(owner["pid"])
    await env.sup.restart(node_id)


async def fix_disk_full(env: Env, node_id: str) -> None:
    data = env.sup.paths(node_id).data
    for pattern in SPOOL_GLOBS:
        for path in data.glob(pattern):
            path.unlink()


async def fix_crashed_worker(env: Env, node_id: str) -> None:
    await env.sup.start(node_id)


async def fix_config_syntax(env: Env, node_id: str) -> None:
    spec = env.sup.specs[node_id]
    cfg = canonical_config(spec.id, env.sup.port_of(node_id), env.model)
    cfg["upstream_base_url"] = env.upstream_url
    env.sup.paths(node_id).config.write_text(dump_config(cfg))
    await env.sup.restart(node_id)


async def fix_leaky_worker(env: Env, node_id: str) -> None:
    await _edit(env, node_id, lambda cfg: cfg.update(worker_recycle_after=0))


async def fix_log_permission(env: Env, node_id: str) -> None:
    os.chmod(env.sup.paths(node_id).log, 0o644)
    await env.sup.start(node_id)


async def fix_upstream_storm(env: Env, node_id: str) -> None:
    await _edit(
        env, node_id,
        lambda cfg: cfg.update(worker_concurrency=cfg["upstream_limit"], retry_backoff=True),
    )


FIXES: dict[str, Callable[[Env, str], Awaitable[None]]] = {
    "model_id_case": fix_model_id_case,
    "stale_endpoint": fix_stale_endpoint,
    "starved_tokens": fix_starved_tokens,
    "forbidden_param": fix_forbidden_param,
    "bad_key_env": fix_bad_key_env,
    "port_squatter": fix_port_squatter,
    "disk_full": fix_disk_full,
    "crashed_worker": fix_crashed_worker,
    "config_syntax": fix_config_syntax,
    "leaky_worker": fix_leaky_worker,
    "log_permission": fix_log_permission,
    "upstream_storm": fix_upstream_storm,
}
assert set(FIXES) == set(faults.ALL_CLASSES)

# For these classes a plain restart looks healthy at first, so it is checked explicitly.
PLAIN_RESTART_CHECKED = ("leaky_worker", "upstream_storm")


@dataclass
class Outcome:
    fault_class: str
    node_id: str
    params: dict
    after_inject: Verdict
    plain_restart_probe_a_ok: bool | None
    after_plain_restart: Verdict | None
    after_fix: Verdict
    exit_code_after_inject: int | None
    log_after_inject: str


async def run_scenario(env: Env, node_id: str, fault_class: str, seed: int) -> Outcome:
    fault = await faults.inject(env.sup, node_id, fault_class, seed)
    status = env.sup.status(node_id)
    log = "\n".join(env.sup.tail_log(node_id, 40) + env.sup.stderr_tail(node_id, 10))
    broken = await env.referee.verify(node_id)

    plain_a = plain = None
    if fault_class in PLAIN_RESTART_CHECKED:
        await env.sup.restart(node_id)
        plain = await env.referee.verify(node_id)
        plain_a = plain.probes[0].ok

    await FIXES[fault_class](env, node_id)
    fixed = await env.referee.verify(node_id)
    return Outcome(fault_class, node_id, fault.params, broken, plain_a, plain, fixed, status.exit_code, log)


async def run_all(env: Env, node_ids: list[str], seed: int = 7) -> dict[str, Outcome]:
    classes = list(faults.ALL_CLASSES)
    assert len(node_ids) >= len(classes)
    await env.sup.start_all()
    try:
        results = await asyncio.gather(
            *(run_scenario(env, node, cls, seed + i) for i, (node, cls) in enumerate(zip(node_ids, classes)))
        )
    finally:
        await env.sup.stop_all()
        for pid in list(env.sup.squatters()):
            await env.sup.kill_squatter(pid)
    return {r.fault_class: r for r in results}


# Fragment expected in the first failing probe's detail after injection, and the node's exit code if it is down.
EXPECTED = {
    "model_id_case": ("HTTP 404", None),
    "stale_endpoint": (("HTTP 404", "HTTP 502"), None),
    "starved_tokens": ("empty content", None),
    "forbidden_param": ("HTTP 403", None),
    "bad_key_env": ("HTTP 401", None),
    "port_squatter": ("connection closed", 4),
    "disk_full": ("HTTP 507", None),
    "crashed_worker": ("connection refused", -9),
    "config_syntax": ("connection refused", 2),
    "leaky_worker": (None, None),
    "log_permission": ("connection refused", 3),
    "upstream_storm": ("HTTP 429", None),
}
EXPECTED_LOG = {
    "port_squatter": "bind failed: address in use",
    "config_syntax": "config parse error line ",
    "log_permission": "Permission denied",
}


def check_outcome(outcome: Outcome) -> None:
    cls = outcome.fault_class
    detail, exit_code = EXPECTED[cls]
    broken = outcome.after_inject
    assert broken.verdict == "rejected", f"{cls}: verify passed on a faulted node\n{broken.evidence()}"
    if detail is None:
        # leaky_worker: the first probe passes, a later one does not.
        assert broken.probes[0].ok and not all(p.ok for p in broken.probes), broken.evidence()
    else:
        first_failure = next(p for p in broken.probes if not p.ok)
        fragments = (detail,) if isinstance(detail, str) else detail
        assert any(f in first_failure.detail for f in fragments), f"{cls}: {first_failure.detail}"
    if exit_code is not None:
        assert outcome.exit_code_after_inject == exit_code, f"{cls}: exit code {outcome.exit_code_after_inject}"
    if cls in EXPECTED_LOG:
        assert EXPECTED_LOG[cls] in outcome.log_after_inject, f"{cls}: {outcome.log_after_inject}"
    if cls in PLAIN_RESTART_CHECKED:
        assert outcome.plain_restart_probe_a_ok, f"{cls}: a plain restart should pass probe A"
        assert outcome.after_plain_restart is not None and outcome.after_plain_restart.verdict == "rejected", (
            f"{cls}: a plain restart must not verify\n{outcome.after_plain_restart.evidence()}"
        )
    assert outcome.after_fix.verdict == "verified", f"{cls}: fix did not verify\n{outcome.after_fix.evidence()}"
