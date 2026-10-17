"""Confirms model ids against GET /v1/models and writes config/models.json."""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import sys
import textwrap
import time
import uuid
from typing import Any

import httpx
from openai import APIStatusError, AsyncOpenAI

from config import settings
from understudy.db import Database
from understudy.llm import LLMClient, parse_action

KEYS = ("super", "lightning", "nano", "ultra", "omni")
SMOKE_KEYS = ("super", "lightning", "nano")
TEMPLATE_REJECTED = (400, 403, 422)

SMOKE_SYSTEM = (
    "You are an on-call responder for one inference node. Reply with exactly one JSON object and nothing else: "
    '{"thought": "<one sentence>", "action": "<tool name>", "args": {}}. '
    "Tools: node_status(), tail_log(n), read_config(), restart_node(), claim_fixed(summary, root_cause)."
)
SMOKE_USER = "Node r2-03 answers every request with HTTP 404 model_not_found. Choose your first action."


def canonical(model_id: str) -> str:
    org, _, name = model_id.lower().partition("/")
    return f"{org}/{name.removeprefix('nvidia-')}"


def resolve(wanted: str, listed: list[str]) -> tuple[str | None, str, list[str]]:
    if wanted in listed:
        return wanted, "exact", []
    by_lower = {i.lower(): i for i in listed}
    if wanted.lower() in by_lower:
        return by_lower[wanted.lower()], "case differs", []
    by_canonical = {canonical(i): i for i in listed}
    if canonical(wanted) in by_canonical:
        return by_canonical[canonical(wanted)], "prefix or case differs", []
    near = difflib.get_close_matches(canonical(wanted), list(by_canonical), n=3, cutoff=0.5)
    return None, "not listed", [by_canonical[n] for n in near]


def snippet(text: str, width: int = 90) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3] + "..."


def price(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def list_models(api_key: str) -> tuple[list[dict[str, Any]], httpx.Headers]:
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(f"{settings.CRUSOE_BASE_URL}/models", headers={"Authorization": f"Bearer {api_key}"})
    if response.status_code in (401, 403):
        print(f"The API rejected {settings.API_KEY_ENV} (HTTP {response.status_code}). Check .env.")
        raise SystemExit(2)
    response.raise_for_status()
    return response.json()["data"], response.headers


async def smoke(client: AsyncOpenAI, model_id: str, thinking: bool) -> dict[str, Any]:
    """One JSON-action request. Falls back through the template-kwargs variants to see which the model accepts."""
    variants: list[tuple[str, dict[str, Any] | None]] = [
        ("full", {"enable_thinking": thinking, "force_nonempty_content": True}),
        ("no_force", {"enable_thinking": thinking}),
        ("none", None),
    ]
    rejected: list[str] = []
    for name, kwargs in variants:
        started = time.monotonic()
        try:
            raw = await client.chat.completions.with_raw_response.create(
                model=model_id,
                messages=[{"role": "system", "content": SMOKE_SYSTEM}, {"role": "user", "content": SMOKE_USER}],
                max_tokens=2048 if thinking else 256,
                temperature=0.6,
                extra_body={"chat_template_kwargs": kwargs} if kwargs else None,
            )
        except APIStatusError as exc:
            if exc.status_code in TEMPLATE_REJECTED:
                rejected.append(f"{name}: HTTP {exc.status_code} {snippet(str(exc.body), 80)}")
                continue
            return {"error": f"{type(exc).__name__}: {snippet(str(exc), 160)}", "rejected": rejected}
        except Exception as exc:  # noqa: BLE001 - report and keep checking the other models
            return {"error": f"{type(exc).__name__}: {snippet(str(exc), 160)}", "rejected": rejected}
        completion = raw.parse()
        choice = completion.choices[0]
        message = choice.message.model_dump()
        content = message.get("content") or ""
        field = next((f for f in ("reasoning_content", "reasoning") if message.get(f)), None)
        return {
            "variant": name,
            "rejected": rejected,
            "latency_ms": (time.monotonic() - started) * 1000,
            "finish_reason": choice.finish_reason,
            "content": content,
            "reasoning_len": len(message.get(field) or "") if field else 0,
            "reasoning_field": field,
            "completion_tokens": completion.usage.completion_tokens if completion.usage else None,
            "action": parse_action(content),
            "ratelimit_headers": any(h.startswith("x-ratelimit") for h in raw.headers),
            "echoed_model": completion.model,
        }
    return {"error": "every chat_template_kwargs variant was rejected", "rejected": rejected}


def build_prefix(lines: int = 34) -> str:
    # A fresh nonce keeps an earlier run's cache from making the first call look warm.
    rows = [
        f"Card {i:03d}: when symptom code S{i:03d} shows up in the node log, read config key k{i:03d}, compare it with "
        f"the canonical value, and restart only after the write has succeeded."
        for i in range(lines)
    ]
    return f"You are an on-call responder (session {uuid.uuid4().hex[:8]}). Reference cards follow.\n" + "\n".join(rows)


async def cache_test(client: LLMClient) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": build_prefix()},
        {"role": "user", "content": "Reply with exactly: OK"},
    ]
    runs = []
    for _ in range(2):
        runs.append(await client.chat("check_models", "super", messages, thinking=False, max_tokens=16,
                                      meta={"purpose": "prefix cache test"}))
        await asyncio.sleep(1.5)
    if runs[-1].cached_tokens == 0:
        await asyncio.sleep(4)
        runs.append(await client.chat("check_models", "super", messages, thinking=False, max_tokens=16,
                                      meta={"purpose": "prefix cache test, third try"}))
    last = runs[-1]
    return {
        "calls": len(runs),
        "prompt_tokens": [r.prompt_tokens for r in runs],
        "cached_tokens": [r.cached_tokens for r in runs],
        "hit_rate": round(last.cached_tokens / last.prompt_tokens, 3) if last.prompt_tokens else 0.0,
        "cost_usd": round(sum(r.cost_usd for r in runs), 6),
    }


def write_models(document: dict[str, Any]) -> None:
    settings.MODELS_JSON.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


async def main() -> int:
    api_key = os.environ.get(settings.API_KEY_ENV, "")
    if not api_key:
        print(f"{settings.API_KEY_ENV} is not set. Add it to .env.")
        return 2

    data, headers = await list_models(api_key)
    listed = sorted(m["id"] for m in data)
    by_id = {m["id"]: m for m in data}
    print(f"GET /v1/models: {len(listed)} models listed")
    print("x-ratelimit headers on /v1/models:", "yes" if any(h.startswith("x-ratelimit") for h in headers) else "no")

    document: dict[str, Any] = {
        "checked_at": time.time(),
        "base_url": settings.CRUSOE_BASE_URL,
        "ratelimit_headers": False,
        "models": {},
    }
    print("\n== model ids ==")
    for key in KEYS:
        wanted = settings.MODELS[key].id
        found, how, near = resolve(wanted, listed)
        entry: dict[str, Any] = {"id": found, "requested_id": wanted, "match": how}
        document["models"][key] = entry
        if found is None:
            print(f"{key:10s} NOT AVAILABLE  (wanted {wanted})")
            for candidate in near:
                print(f"{'':10s} near match, not used: {candidate}")
            continue
        info = by_id[found]
        params = sorted(info.get("supported_parameters") or [])
        features = info.get("features") or []
        modality = (info.get("architecture") or {}).get("modality")
        entry.update(
            context_length=info.get("context_length"),
            max_completion_tokens=(info.get("top_provider") or {}).get("max_completion_tokens"),
            features=info.get("features"),
            tags=info.get("tags") or [],
            modality=modality,
            quantization=info.get("quantization"),
            supported_parameters=params,
            pricing=info.get("pricing"),
        )
        print(f"{key:10s} {found}  [{how}]")
        print(f"{'':10s} context_length={info.get('context_length')}  modality={modality}  "
              f"quantization={info.get('quantization')}  tags={info.get('tags')}  "
              f"features={features if features else 'not reported by this endpoint'}")
        print(textwrap.fill(", ".join(params), width=110, initial_indent=" " * 10 + "supported_parameters: ",
                            subsequent_indent=" " * 12))
        api_price = info.get("pricing") or {}
        spec = settings.MODELS[key]
        pairs = ((api_price.get("prompt"), spec.input_per_m), (api_price.get("completion"), spec.output_per_m),
                 (api_price.get("input_cache_reads"), spec.cached_per_m))
        if any(price(a) is not None and abs(price(a) - b) > 1e-9 for a, b in pairs):
            print(f"{'':10s} WARNING: API price {api_price.get('prompt')}/{api_price.get('completion')}/"
                  f"{api_price.get('input_cache_reads')} differs from the table in config/settings.py")

    problems: list[str] = []
    missing = [k for k in SMOKE_KEYS if document["models"][k]["id"] is None]
    if missing:
        problems.append(f"required models not available: {', '.join(missing)}")

    print("\n== smoke tests (JSON action prompt) ==")
    sdk = AsyncOpenAI(api_key=api_key, base_url=settings.CRUSOE_BASE_URL, max_retries=3, timeout=120)
    jobs = [(k, t) for k in SMOKE_KEYS if document["models"][k]["id"] for t in (False, True)]
    outcomes = await asyncio.gather(*(smoke(sdk, document["models"][k]["id"], t) for k, t in jobs))
    await sdk.close()
    by_job = dict(zip(jobs, outcomes))
    for (key, thinking), out in by_job.items():
        label = f"{key:10s} thinking={'on ' if thinking else 'off'}"
        if "error" in out:
            print(f"{label} ERROR {out['error']}")
            for line in out["rejected"]:
                print(f"{'':25s} rejected {line}")
            problems.append(f"{key} thinking={thinking}: {out['error']}")
            continue
        action = out["action"]
        print(f"{label} {out['latency_ms']:7.0f} ms  finish={out['finish_reason']}  tokens={out['completion_tokens']}  "
              f"reasoning_chars={out['reasoning_len']}  template_kwargs={out['variant']} (accepted)")
        print(f"{'':25s} content: {snippet(out['content'])!r}")
        print(f"{'':25s} parsed action: {action.get('action') if action else None}")
        for line in out["rejected"]:
            print(f"{'':25s} rejected before: {line}")
        if not action or "action" not in action:
            problems.append(f"{key} thinking={thinking}: reply was not a JSON action")
        if not out["content"].strip():
            problems.append(f"{key} thinking={thinking}: empty content")

    for key in SMOKE_KEYS:
        runs = [by_job[(key, t)] for t in (False, True) if (key, t) in by_job and "error" not in by_job[(key, t)]]
        if not runs:
            continue
        entry = document["models"][key]
        variants = {r["variant"] for r in runs}
        entry["chat_template_kwargs"] = "none" not in variants
        entry["force_nonempty_content"] = variants == {"full"}
        off, on = by_job.get((key, False), {}), by_job.get((key, True), {})
        if "error" not in off and "error" not in on:
            entry["thinking_effective"] = on["reasoning_len"] > 0 and off["reasoning_len"] == 0
        entry["reasoning_field"] = next((r["reasoning_field"] for r in runs if r["reasoning_field"]), None)
        document["ratelimit_headers"] = document["ratelimit_headers"] or any(r["ratelimit_headers"] for r in runs)
        echoed = {r["echoed_model"] for r in runs}
        if echoed != {entry["id"]}:
            problems.append(f"{key}: response model {sorted(echoed)} does not equal the requested id")
    write_models(document)

    print("\n== prefix cache (two identical SUPER calls) ==")
    if document["models"]["super"]["id"]:
        db = await Database.open()
        client = LLMClient(db)
        try:
            cache = await cache_test(client)
            document["models"]["super"]["prefix_cache"] = cache
            print(f"prompt_tokens={cache['prompt_tokens']}  cached_tokens={cache['cached_tokens']}  "
                  f"hit_rate={cache['hit_rate']:.1%}  calls={cache['calls']}  cost=${cache['cost_usd']:.6f}")
            print(f"spent so far on this database: ${await db.total_cost():.4f} of ${settings.BUDGET_USD:.2f}")
        finally:
            await client.aclose()
            await db.close()

    document["ratelimit_headers"] = bool(document["ratelimit_headers"])
    write_models(document)
    print(f"\nwrote {settings.MODELS_JSON.relative_to(settings.ROOT)}")
    print("ultra:", document["models"]["ultra"]["id"] or "not available, SUPER is used instead")
    print("omni: ", document["models"]["omni"]["id"] or "not available")
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
