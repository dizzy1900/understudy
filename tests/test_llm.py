import json
import logging
from contextlib import asynccontextmanager

import httpx
import pytest
from openai import AuthenticationError, InternalServerError

from understudy.db import Database
from understudy.llm import (
    BudgetExceeded,
    LLMClient,
    ModelUnavailable,
    TokenBucket,
    cost_usd,
)

MODELS = {
    "super": {"id": "vendor/Super"},
    "lightning": {"id": "vendor/Lightning"},
    "nano": {"id": "vendor/Nano"},
    "ultra": {"id": None},
    "omni": {"id": None},
}
MESSAGES = [{"role": "user", "content": "hi"}]


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def completion(content="OK", finish="stop", prompt=100, output=10, cached=40, **message):
    return httpx.Response(
        200,
        json={
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "vendor/Super",
            "choices": [
                {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content, **message}}
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": output,
                "total_tokens": prompt + output,
                "prompt_tokens_details": {"cached_tokens": cached} if cached is not None else None,
            },
        },
    )


def failure(status, message="nope", headers=None):
    return httpx.Response(status, json={"errors": [message]}, headers=headers)


class FakeServer:
    """Replies with the scripted responses in order; the last one repeats."""

    def __init__(self, *responses):
        self.responses = list(responses) or [completion()]
        self.requests: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


@asynccontextmanager
async def make_client(tmp_path, server, **kwargs):
    db = await Database.open(tmp_path / "t.db")
    fake = FakeTime()
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    client = LLMClient(
        db,
        api_key="test-key",
        models=kwargs.pop("models", MODELS),
        http_client=http,
        clock=fake.clock,
        sleep=fake.sleep,
        **kwargs,
    )
    try:
        yield client, db, fake
    finally:
        await client.aclose()
        await db.close()


def test_cost_uses_the_price_table():
    # 600 uncached input at 0.30, 400 cached at 0.15, 100 output at 2.40 (USD per million)
    assert cost_usd("super", 1000, 100, 400) == pytest.approx((600 * 0.30 + 400 * 0.15 + 100 * 2.40) / 1e6)
    assert cost_usd("nano", 1000, 0, 0) == pytest.approx(1000 * 0.05 / 1e6)
    assert cost_usd("nano", 10, 0, 999) == pytest.approx(10 * 0.03 / 1e6)


@pytest.mark.anyio
async def test_chat_returns_usage_and_logs_the_call(tmp_path):
    server = FakeServer(completion("hello", reasoning_content="because"))
    async with make_client(tmp_path, server) as (client, db, _):
        result = await client.chat(
            "responder", "super", MESSAGES, thinking=False, max_tokens=256, meta={"incident_id": 4, "agent_id": "r-1", "k": 1}
        )
        assert (result.content, result.reasoning, result.finish_reason) == ("hello", "because", "stop")
        assert (result.prompt_tokens, result.completion_tokens, result.cached_tokens) == (100, 10, 40)
        assert result.cost_usd == pytest.approx(cost_usd("super", 100, 10, 40))
        assert result.model == "vendor/Super"

        body = server.requests[0]
        assert body["model"] == "vendor/Super"
        assert body["chat_template_kwargs"] == {"enable_thinking": False, "force_nonempty_content": True}
        assert "thinking" not in body

        row = await db.fetchone("SELECT * FROM llm_calls")
        assert (row["role"], row["model_key"], row["incident_id"], row["agent_id"]) == ("responder", "super", 4, "r-1")
        assert row["cached_tokens"] == 40 and row["ok"] == 1
        assert row["meta_json"] == '{"k":1}'
        assert row["cost_usd"] == pytest.approx(result.cost_usd)


@pytest.mark.anyio
async def test_reads_the_alternate_reasoning_field_and_missing_cache_details(tmp_path):
    server = FakeServer(completion("x", reasoning="thought", cached=None))
    async with make_client(tmp_path, server) as (client, _, _):
        result = await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert result.reasoning == "thought"
        assert result.cached_tokens == 0


@pytest.mark.anyio
async def test_thinking_raises_the_token_floor(tmp_path):
    server = FakeServer()
    async with make_client(tmp_path, server) as (client, _, _):
        await client.chat("r", "super", MESSAGES, thinking=True, max_tokens=100)
    assert server.requests[0]["max_tokens"] == 2048
    assert server.requests[0]["chat_template_kwargs"]["enable_thinking"] is True


@pytest.mark.anyio
async def test_retries_429_with_doubling_backoff(tmp_path):
    server = FakeServer(failure(429), failure(503), failure(429), completion())
    async with make_client(tmp_path, server) as (client, db, fake):
        result = await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert result.content == "OK"
        assert len(server.requests) == 4
        waits = [s for s in fake.sleeps if s >= 1]
        assert 1 <= waits[0] < 1.6 and 2 <= waits[1] < 2.6 and 4 <= waits[2] < 4.6
        row = await db.fetchone("SELECT * FROM llm_calls")
        assert row["retries"] == 3


@pytest.mark.anyio
async def test_retry_after_header_is_honoured(tmp_path):
    server = FakeServer(failure(429, headers={"retry-after": "7"}), completion())
    async with make_client(tmp_path, server) as (client, _, fake):
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
    assert max(fake.sleeps) >= 7


@pytest.mark.anyio
async def test_only_a_server_stated_wait_holds_back_other_callers(tmp_path):
    server = FakeServer(failure(429), completion(), failure(429, headers={"retry-after": "7"}), completion())
    async with make_client(tmp_path, server) as (client, _, _):
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert client._limiters["nano"].requests._blocked_until == 0.0
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert client._limiters["nano"].requests._blocked_until > 0.0


@pytest.mark.anyio
async def test_gives_up_after_six_tries_and_logs_the_failure(tmp_path):
    server = FakeServer(failure(503))
    async with make_client(tmp_path, server) as (client, db, fake):
        with pytest.raises(InternalServerError):
            await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert len(server.requests) == 6
        assert len([s for s in fake.sleeps if s >= 1]) == 5
        row = await db.fetchone("SELECT * FROM llm_calls")
        assert row["ok"] == 0 and "InternalServerError" in row["error"] and row["cost_usd"] == 0


@pytest.mark.anyio
async def test_connection_errors_are_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("slow", request=request)
        return completion()

    async with make_client(tmp_path, handler) as (client, _, _):
        assert (await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)).content == "OK"
    assert len(calls) == 3


@pytest.mark.anyio
async def test_other_errors_are_not_retried(tmp_path):
    server = FakeServer(failure(401, "Authentication failed"))
    async with make_client(tmp_path, server) as (client, db, _):
        with pytest.raises(AuthenticationError):
            await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert len(server.requests) == 1
        assert await db.scalar("SELECT ok FROM llm_calls") == 0


@pytest.mark.anyio
async def test_force_nonempty_rejection_is_remembered_per_model(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        server.requests.append(body)
        if "force_nonempty_content" in body.get("chat_template_kwargs", {}):
            return failure(400, "unknown chat_template_kwargs key force_nonempty_content")
        return completion()

    server = FakeServer()
    async with make_client(tmp_path, handler) as (client, db, fake):
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert [("force_nonempty_content" in r["chat_template_kwargs"]) for r in server.requests] == [True, False]
        assert fake.sleeps == []  # not counted as a transient retry

        server.requests.clear()
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        await client.chat("r", "super", MESSAGES, thinking=False, max_tokens=64)
        flags = [("force_nonempty_content" in r["chat_template_kwargs"]) for r in server.requests]
        assert flags == [False, True, False]  # super is tried with it first, then drops it
        assert await db.scalar("SELECT COUNT(*) FROM llm_calls WHERE ok = 1") == 3


@pytest.mark.anyio
async def test_models_json_flags_skip_force_nonempty_and_template(tmp_path):
    models = {**MODELS, "nano": {"id": "vendor/Nano", "force_nonempty_content": False}, "super": {"id": "vendor/Super", "chat_template_kwargs": False}}
    server = FakeServer()
    async with make_client(tmp_path, server, models=models) as (client, _, _):
        await client.chat("r", "nano", MESSAGES, thinking=True, max_tokens=64)
        await client.chat("r", "super", MESSAGES, thinking=True, max_tokens=64)
    assert server.requests[0]["chat_template_kwargs"] == {"enable_thinking": True}
    assert "chat_template_kwargs" not in server.requests[1]


@pytest.mark.anyio
async def test_empty_length_finish_retries_once_with_double_max_tokens(tmp_path):
    server = FakeServer(completion("", finish="length", output=512), completion("done", output=20))
    async with make_client(tmp_path, server) as (client, db, _):
        result = await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=512)
        assert [r["max_tokens"] for r in server.requests] == [512, 1024]
        assert (result.content, result.attempts, result.completion_tokens) == ("done", 2, 532)
        assert result.cost_usd == pytest.approx(2 * cost_usd("nano", 100, 0, 40) + cost_usd("nano", 0, 532, 0))
        assert await db.scalar("SELECT COUNT(*) FROM llm_calls") == 2


@pytest.mark.anyio
async def test_length_retry_is_capped_and_only_for_empty_content(tmp_path):
    server = FakeServer(completion("", finish="length"), completion("", finish="length"))
    async with make_client(tmp_path, server) as (client, _, _):
        result = await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=6000)
        assert [r["max_tokens"] for r in server.requests] == [6000, 8192]
        assert result.finish_reason == "length" and result.attempts == 2

    server = FakeServer(completion("partial", finish="length"))
    async with make_client(tmp_path, server) as (client, _, _):
        assert (await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)).attempts == 1
        assert len(server.requests) == 1

    server = FakeServer(completion("", finish="length"))
    async with make_client(tmp_path, server) as (client, _, _):
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=8192)
        assert len(server.requests) == 1


@pytest.mark.anyio
async def test_budget_blocks_calls_once_reached(tmp_path):
    server = FakeServer(completion(prompt=100, output=10, cached=0))
    async with make_client(tmp_path, server, budget_usd=1e-4) as (client, _, _):
        await client.chat("r", "super", MESSAGES, thinking=False, max_tokens=64)  # about 5.4e-5
        await client.chat("r", "super", MESSAGES, thinking=False, max_tokens=64)
        with pytest.raises(BudgetExceeded) as caught:
            await client.chat("r", "super", MESSAGES, thinking=False, max_tokens=64)
        assert caught.value.spent >= 1e-4
        assert len(server.requests) == 2


@pytest.mark.anyio
async def test_budget_counts_spend_already_in_the_database(tmp_path):
    server = FakeServer()
    async with make_client(tmp_path, server, budget_usd=1.0) as (client, db, _):
        await db.record_llm_call(role="earlier", model_key="super", model="m", cost_usd=1.5)
        with pytest.raises(BudgetExceeded):
            await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert server.requests == []


@pytest.mark.anyio
async def test_ultra_maps_to_super_when_unavailable_and_logs_once(tmp_path, caplog):
    server = FakeServer()
    caplog.set_level(logging.WARNING, logger="understudy.llm")
    async with make_client(tmp_path, server) as (client, db, _):
        first = await client.chat("grader", "ultra", MESSAGES, thinking=False, max_tokens=64)
        await client.chat("grader", "ultra", MESSAGES, thinking=False, max_tokens=64)
        assert server.requests[0]["model"] == "vendor/Super"
        assert first.model_key == "super"
        assert await db.scalar("SELECT DISTINCT model_key FROM llm_calls") == "super"
    assert len([r for r in caplog.records if "ultra" in r.getMessage()]) == 1


@pytest.mark.anyio
async def test_ultra_is_used_when_listed(tmp_path):
    models = {**MODELS, "ultra": {"id": "vendor/Ultra"}}
    server = FakeServer()
    async with make_client(tmp_path, server, models=models) as (client, _, _):
        await client.chat("grader", "ultra", MESSAGES, thinking=False, max_tokens=64)
    assert server.requests[0]["model"] == "vendor/Ultra"


@pytest.mark.anyio
async def test_unavailable_model_without_a_fallback_raises(tmp_path):
    async with make_client(tmp_path, FakeServer()) as (client, _, _):
        with pytest.raises(ModelUnavailable):
            await client.chat("r", "omni", MESSAGES, thinking=False, max_tokens=64)
        with pytest.raises(KeyError):
            await client.chat("r", "nope", MESSAGES, thinking=False, max_tokens=64)


@pytest.mark.anyio
async def test_missing_models_json_falls_back_to_configured_ids(tmp_path, monkeypatch, caplog):
    from config import settings

    monkeypatch.setattr(settings, "MODELS_JSON", tmp_path / "absent.json")
    server = FakeServer()
    db = await Database.open(tmp_path / "t.db")
    http = httpx.AsyncClient(transport=httpx.MockTransport(server))
    client = LLMClient(db, api_key="k", http_client=http)
    await client.chat("r", "lightning", MESSAGES, thinking=False, max_tokens=64)
    assert server.requests[0]["model"] == settings.MODELS["lightning"].id
    await client.aclose()
    await db.close()


@pytest.mark.anyio
async def test_server_ratelimit_headers_slow_the_next_call(tmp_path):
    headers = {"x-ratelimit-remaining-requests": "2", "x-ratelimit-limit-requests": "600"}
    ok = completion()
    ok.headers.update(headers)
    server = FakeServer(ok)
    async with make_client(tmp_path, server) as (client, _, fake):
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert fake.sleeps == []
        await client.chat("r", "nano", MESSAGES, thinking=False, max_tokens=64)
        assert fake.sleeps and fake.sleeps[0] >= 0.9


@pytest.mark.anyio
async def test_token_bucket_waits_for_refill():
    fake = FakeTime()
    bucket = TokenBucket(60, burst_fraction=0.25, clock=fake.clock, sleep=fake.sleep)  # 1 per second, burst 15
    await bucket.acquire(15)
    assert fake.sleeps == []
    await bucket.acquire(5)
    assert fake.now == pytest.approx(5.0)

    bucket.adjust(3)
    await bucket.acquire(3)
    assert fake.now == pytest.approx(5.0)

    bucket.block_for(10)
    await bucket.acquire(1)
    assert fake.now >= 15.0


@pytest.mark.anyio
async def test_missing_api_key_is_an_error(monkeypatch):
    monkeypatch.delenv("CRUSOE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CRUSOE_API_KEY"):
        LLMClient(None, api_key="")
