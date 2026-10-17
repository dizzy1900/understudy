"""Async client for Crusoe Managed Inference: rate limiting, retries, usage and cost accounting."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from config import settings
from understudy.db import Database

log = logging.getLogger("understudy.llm")

THINKING_MIN_TOKENS = 2048
MAX_TOKENS_CAP = 8192
MAX_TRIES = 6
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0
RETRY_AFTER_CAP_S = 60.0
DEFAULT_RPM = 500
DEFAULT_TPM = 1_800_000
TRANSIENT_STATUS = frozenset({429, 502, 503, 504})
TEMPLATE_REJECTED_STATUS = frozenset({400, 403, 422})
FALLBACKS = {"ultra": "super"}


class BudgetExceeded(RuntimeError):
    def __init__(self, spent: float, budget: float):
        super().__init__(f"LLM budget reached: ${spent:.4f} spent of ${budget:.2f}")
        self.spent = spent
        self.budget = budget


class ModelUnavailable(RuntimeError):
    pass


@dataclass
class LLMResult:
    """Outcome of chat(). When a call was retried for an empty "length" finish, tokens, cost and
    latency are totals over both requests and `attempts` is 2."""

    content: str
    reasoning: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    latency_ms: float
    model: str = ""
    model_key: str = ""
    attempts: int = 1


def cost_usd(model_key: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    price = settings.MODELS[model_key]
    cached = min(cached_tokens, prompt_tokens)
    return (
        (prompt_tokens - cached) * price.input_per_m
        + cached * price.cached_per_m
        + completion_tokens * price.output_per_m
    ) / 1_000_000


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Deliberately high (3 chars per token) so the bucket errs toward waiting."""
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return chars // 3 + 8 * len(messages)


class TokenBucket:
    def __init__(
        self,
        per_minute: float,
        *,
        burst_fraction: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.rate = per_minute / 60.0
        self.capacity = max(1.0, per_minute * burst_fraction)
        self._level = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._stamp = clock()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._level = min(self.capacity, self._level + (now - self._stamp) * self.rate)
        self._stamp = now

    async def acquire(self, amount: float = 1.0) -> None:
        amount = min(amount, self.capacity)
        # Holding the lock while waiting keeps waiters in arrival order.
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._blocked_until:
                    await self._sleep(self._blocked_until - now)
                    continue
                self._refill()
                if self._level >= amount:
                    self._level -= amount
                    return
                await self._sleep((amount - self._level) / self.rate)

    def adjust(self, delta: float) -> None:
        """Positive refunds unused budget; negative charges for an underestimate."""
        self._refill()
        self._level = min(self.capacity, self._level + delta)

    def clamp(self, remaining: float) -> None:
        self._refill()
        self._level = min(self._level, float(remaining))

    def block_for(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    @property
    def level(self) -> float:
        self._refill()
        return self._level


def _header_int(headers: Any, name: str) -> int | None:
    try:
        return int(float(headers.get(name)))
    except (TypeError, ValueError):
        return None


class ModelLimiter:
    """Requests-per-minute and tokens-per-minute buckets for one model."""

    def __init__(self, rpm: float, tpm: float, **bucket_kwargs: Any):
        self.requests = TokenBucket(rpm, **bucket_kwargs)
        self.tokens = TokenBucket(tpm, **bucket_kwargs)

    async def acquire(self, estimated_tokens: float) -> None:
        await self.requests.acquire(1)
        await self.tokens.acquire(estimated_tokens)

    def block_for(self, seconds: float) -> None:
        self.requests.block_for(seconds)
        self.tokens.block_for(seconds)

    def observe(self, headers: Any) -> None:
        """Slows down when the server reports little headroom. The headers are not always sent."""
        for bucket, kind in ((self.requests, "requests"), (self.tokens, "tokens")):
            remaining = _header_int(headers, f"x-ratelimit-remaining-{kind}")
            if remaining is None:
                continue
            bucket.clamp(remaining)
            limit = _header_int(headers, f"x-ratelimit-limit-{kind}")
            if limit and remaining < 0.1 * limit:
                self.block_for(1.0)


def _load_models(path: Any) -> dict[str, dict[str, Any]] | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    return data.get("models", {})


def _retry_after(headers: Any) -> float | None:
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


class LLMClient:
    def __init__(
        self,
        db: Database | None = None,
        *,
        api_key: str | None = None,
        base_url: str = settings.CRUSOE_BASE_URL,
        budget_usd: float | None = settings.BUDGET_USD,
        models: dict[str, dict[str, Any]] | None = None,
        rpm: float = DEFAULT_RPM,
        tpm: float = DEFAULT_TPM,
        limits: dict[str, tuple[float, float]] | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._api_key = api_key or os.environ.get(settings.API_KEY_ENV, "")
        if not self._api_key:
            raise RuntimeError(f"{settings.API_KEY_ENV} is not set")
        # The SDK's own retries are off so every retry is counted and logged here.
        self._client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=base_url,
            max_retries=0,
            timeout=httpx.Timeout(timeout, connect=10.0),
            http_client=http_client,
        )
        self._db = db
        self._budget = budget_usd
        self._models = models if models is not None else _load_models(settings.MODELS_JSON)
        self._rate = (rpm, tpm)
        self._limits = limits or {}
        self._clock = clock
        self._sleep = sleep
        self._limiters: dict[str, ModelLimiter] = {}
        self._no_force: set[str] = set()
        self._warned: set[str] = set()
        self._spent = 0.0
        self._synced_at = float("-inf")

    async def aclose(self) -> None:
        await self._client.close()

    # model resolution -------------------------------------------------------------

    def _warn_once(self, tag: str, message: str, *args: Any) -> None:
        if tag not in self._warned:
            self._warned.add(tag)
            log.warning(message, *args)

    def resolve(self, key: str) -> tuple[str, str]:
        """Returns (model key actually used, exact model id)."""
        if key not in settings.MODELS:
            raise KeyError(f"unknown model key {key!r}")
        if self._models is None:
            self._warn_once("no-models-json", "config/models.json is missing; using configured ids (run make check-models)")
            return key, settings.MODELS[key].id
        model_id = (self._models.get(key) or {}).get("id")
        if model_id:
            return key, model_id
        fallback = FALLBACKS.get(key)
        if fallback is None:
            raise ModelUnavailable(f"model {key!r} is not available to this project")
        self._warn_once(f"fallback-{key}", "%s is not available; using %s instead", key, fallback)
        return self.resolve(fallback)

    def _limiter(self, key: str) -> ModelLimiter:
        if key not in self._limiters:
            rpm, tpm = self._limits.get(key, self._rate)
            self._limiters[key] = ModelLimiter(rpm, tpm, clock=self._clock, sleep=self._sleep)
        return self._limiters[key]

    def _extra_body(self, key: str, thinking: bool) -> dict[str, Any] | None:
        flags = (self._models or {}).get(key) or {}
        if flags.get("chat_template_kwargs") is False:
            return None
        kwargs: dict[str, Any] = {"enable_thinking": bool(thinking)}
        if key not in self._no_force and flags.get("force_nonempty_content") is not False:
            kwargs["force_nonempty_content"] = True
        return {"chat_template_kwargs": kwargs}

    def _force_rejected(self, key: str, extra: dict[str, Any] | None, exc: APIStatusError) -> bool:
        if not extra or "force_nonempty_content" not in extra["chat_template_kwargs"]:
            return False
        if exc.status_code not in TEMPLATE_REJECTED_STATUS:
            return False
        text = f"{exc} {exc.body}"
        if "force_nonempty_content" not in text and "chat_template_kwargs" not in text:
            return False
        self._no_force.add(key)
        self._warn_once(f"force-{key}", "%s rejected force_nonempty_content; sending without it from now on", key)
        return True

    # budget -------------------------------------------------------------------------

    @property
    def budget(self) -> float | None:
        return self._budget

    async def spent(self) -> float:
        if self._db is not None and self._clock() - self._synced_at >= 2.0:
            self._spent = await self._db.total_cost()
            self._synced_at = self._clock()
        return self._spent

    async def _check_budget(self) -> None:
        if self._budget is None:
            return
        spent = await self.spent()
        if spent >= self._budget:
            raise BudgetExceeded(spent, self._budget)

    # requests -----------------------------------------------------------------------

    async def chat(
        self,
        role: str,
        model_key: str,
        messages: list[dict[str, Any]],
        *,
        thinking: bool,
        max_tokens: int,
        temperature: float = 0.6,
        meta: dict[str, Any] | None = None,
    ) -> LLMResult:
        """One chat completion. `meta` may carry incident_id and agent_id; other keys are stored as JSON.

        Reasoning tokens count toward max_tokens, so thinking calls get at least THINKING_MIN_TOKENS.
        """
        key, model_id = self.resolve(model_key)
        if thinking:
            max_tokens = max(max_tokens, THINKING_MIN_TOKENS)
        await self._check_budget()
        first = await self._once(role, key, model_id, messages, thinking, max_tokens, temperature, meta)
        if first.finish_reason != "length" or first.content.strip() or max_tokens >= MAX_TOKENS_CAP:
            return first
        await self._check_budget()
        second = await self._once(
            role, key, model_id, messages, thinking, min(max_tokens * 2, MAX_TOKENS_CAP), temperature, meta
        )
        return LLMResult(
            content=second.content,
            reasoning=second.reasoning,
            finish_reason=second.finish_reason,
            prompt_tokens=first.prompt_tokens + second.prompt_tokens,
            completion_tokens=first.completion_tokens + second.completion_tokens,
            cached_tokens=first.cached_tokens + second.cached_tokens,
            cost_usd=first.cost_usd + second.cost_usd,
            latency_ms=first.latency_ms + second.latency_ms,
            model=second.model,
            model_key=second.model_key,
            attempts=2,
        )

    async def _once(
        self,
        role: str,
        key: str,
        model_id: str,
        messages: list[dict[str, Any]],
        thinking: bool,
        max_tokens: int,
        temperature: float,
        meta: dict[str, Any] | None,
    ) -> LLMResult:
        meta = dict(meta or {})
        ids = {"incident_id": meta.pop("incident_id", None), "agent_id": meta.pop("agent_id", None)}
        limiter = self._limiter(key)
        estimate = estimate_tokens(messages) + max_tokens
        retries = 0
        try:
            completion, latency_ms, retries = await self._request(
                key, model_id, messages, thinking, max_tokens, temperature, limiter, estimate
            )
        except Exception as exc:
            await self._log_call(
                role, key, model_id, ids, meta, ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}"
            )
            raise
        result = self._parse(completion, key, model_id, latency_ms)
        limiter.tokens.adjust(estimate - (result.prompt_tokens + result.completion_tokens))
        await self._log_call(role, key, model_id, ids, meta, ok=True, result=result, retries=retries)
        self._spent += result.cost_usd
        return result

    async def _request(
        self,
        key: str,
        model_id: str,
        messages: list[dict[str, Any]],
        thinking: bool,
        max_tokens: int,
        temperature: float,
        limiter: ModelLimiter,
        estimate: int,
    ) -> tuple[Any, float, int]:
        retries = 0
        while True:
            extra = self._extra_body(key, thinking)
            await limiter.acquire(estimate)
            started = self._clock()
            retry_after: float | None = None
            try:
                raw = await self._client.chat.completions.with_raw_response.create(
                    model=model_id,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=extra,
                )
            except APIStatusError as exc:
                if self._force_rejected(key, extra, exc):
                    limiter.tokens.adjust(estimate)
                    continue
                if exc.status_code not in TRANSIENT_STATUS:
                    limiter.tokens.adjust(estimate)
                    raise
                retry_after = _retry_after(exc.response.headers)
                error: Exception = exc
            except APIConnectionError as exc:
                error = exc
            else:
                limiter.observe(raw.headers)
                return raw.parse(), (self._clock() - started) * 1000, retries
            retries += 1
            if retries >= MAX_TRIES:
                log.error("%s: giving up after %d tries: %s", model_id, retries, type(error).__name__)
                raise error
            delay = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (retries - 1)) + random.uniform(0, 0.5)
            if retry_after:
                delay = max(delay, min(retry_after, RETRY_AFTER_CAP_S))
            log.warning("%s: %s, retry %d in %.1fs", model_id, type(error).__name__, retries, delay)
            limiter.block_for(delay)
            await self._sleep(delay)

    def _parse(self, completion: Any, key: str, model_id: str, latency_ms: float) -> LLMResult:
        choice = completion.choices[0] if completion.choices else None
        content, reasoning, finish = "", "", "error"
        if choice is not None:
            finish = choice.finish_reason or "unknown"
            message = choice.message.model_dump()
            content = message.get("content") or ""
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        usage = completion.usage
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        output = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = min(int(getattr(details, "cached_tokens", 0) or 0), prompt) if details else 0
        return LLMResult(
            content=content,
            reasoning=reasoning,
            finish_reason=finish,
            prompt_tokens=prompt,
            completion_tokens=output,
            cached_tokens=cached,
            cost_usd=cost_usd(key, prompt, output, cached),
            latency_ms=latency_ms,
            model=model_id,
            model_key=key,
        )

    async def _log_call(
        self,
        role: str,
        key: str,
        model_id: str,
        ids: dict[str, Any],
        meta: dict[str, Any],
        *,
        ok: bool,
        result: LLMResult | None = None,
        retries: int = 0,
        error: str | None = None,
    ) -> None:
        if self._db is None:
            return
        fields: dict[str, Any] = {}
        if result is not None:
            fields = {
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "cached_tokens": result.cached_tokens,
                "cost_usd": result.cost_usd,
                "latency_ms": result.latency_ms,
                "finish_reason": result.finish_reason,
            }
        try:
            await self._db.record_llm_call(
                role=role,
                model_key=key,
                model=model_id,
                ok=ok,
                retries=retries,
                error=error.replace(self._api_key, "***") if error else None,
                meta=meta or None,
                **ids,
                **fields,
            )
        except Exception:
            # The request is already paid for; losing the row is better than losing the answer.
            log.exception("could not record llm call")


_default: LLMClient | None = None


def configure(db: Database | None = None, **kwargs: Any) -> LLMClient:
    global _default
    _default = LLMClient(db, **kwargs)
    return _default


async def chat(
    role: str,
    model_key: str,
    messages: list[dict[str, Any]],
    *,
    thinking: bool,
    max_tokens: int,
    temperature: float = 0.6,
    meta: dict[str, Any] | None = None,
) -> LLMResult:
    if _default is None:
        raise RuntimeError("llm.configure() has not been called")
    return await _default.chat(
        role, model_key, messages, thinking=thinking, max_tokens=max_tokens, temperature=temperature, meta=meta
    )


# action parsing -------------------------------------------------------------------------

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}


def _balanced_end(text: str, start: int) -> int | None:
    depth, quote, i = 0, None, start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _normalize(src: str) -> str:
    """Rewrites single quotes, bare keys, trailing commas and Python literals into JSON, outside of strings only."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "\"'":
            buf: list[str] = []
            i += 1
            while i < n and src[i] != ch:
                if src[i] == "\\" and i + 1 < n:
                    buf.append("'" if (ch == "'" and src[i + 1] == "'") else src[i : i + 2])
                    i += 2
                    continue
                buf.append('\\"' if src[i] == '"' else "\\n" if src[i] == "\n" else src[i])
                i += 1
            i += 1
            out.append('"' + "".join(buf) + '"')
        elif ch == ",":
            j = i + 1
            while j < n and src[j] in " \t\r\n":
                j += 1
            if not (j < n and src[j] in "}]"):
                out.append(ch)
            i += 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            k = j
            while k < n and src[k] in " \t":
                k += 1
            out.append(f'"{word}"' if k < n and src[k] == ":" else _PY_LITERALS.get(word, word))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _load_object(candidate: str) -> dict[str, Any] | None:
    for attempt in (candidate, _normalize(candidate)):
        try:
            value = json.loads(attempt, strict=False)
        except ValueError:
            continue
        return value if isinstance(value, dict) else None
    return None


def parse_action(text: str | None) -> dict[str, Any] | None:
    """Returns the first balanced JSON object in `text`, or None.

    Tolerates code fences, prose around the object, <think> blocks, trailing commas, single-quoted
    strings, Python literals and raw newlines inside strings.
    """
    if not text:
        return None
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    pos = 0
    while True:
        start = text.find("{", pos)
        if start < 0:
            return None
        end = _balanced_end(text, start)
        if end is None:
            return None
        parsed = _load_object(text[start:end])
        if parsed is not None:
            return parsed
        pos = end
