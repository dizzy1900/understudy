import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse

from tests.fleet_support import ThreadedServer, free_port
from understudy.fleet.referee import Referee

pytestmark = pytest.mark.anyio

MODEL = "test/Canonical-Model-1B"


class FakeNode(ThreadedServer):
    """A node whose behaviour the test controls, so each referee check can fail in isolation."""

    def __init__(self):
        super().__init__(FastAPI())
        self.mode = "ok"
        self.served = 0
        self.in_flight = 0
        self.die_after = None
        self.app.post("/v1/chat/completions")(self._chat)

    def completion(self, content="OK", finish="stop", model=MODEL):
        return JSONResponse(
            {"model": model, "choices": [{"message": {"content": content}, "finish_reason": finish}]}
        )

    async def _chat(self):
        self.served += 1
        self.in_flight += 1
        try:
            if self.die_after is not None and self.served > self.die_after:
                return JSONResponse({"error": {"message": "worker gone"}}, status_code=503)
            await asyncio.sleep(0.05)
            mode = self.mode
            if mode == "ok":
                return self.completion()
            if mode == "http404":
                return JSONResponse({"code": "model_not_found", "message": "Model not found: x"}, status_code=404)
            if mode == "http403":
                return JSONResponse({"errors": ["Request blocked: parameter 'x' is not allowed"]}, status_code=403)
            if mode == "plain404":
                return PlainTextResponse("404 page not found", status_code=404)
            if mode == "empty":
                return self.completion(content=None, finish="length")
            if mode == "blank":
                return self.completion(content="  \n")
            if mode == "truncated":
                return self.completion(content="The user says", finish="length")
            if mode == "wrong_model":
                return self.completion(model="other/Model")
            if mode == "garbage":
                return JSONResponse({"hello": "world"})
            if mode == "burst_fails":
                return self.completion() if self.in_flight == 1 else JSONResponse({"code": "rate_limit_exceeded", "message": "too many"}, status_code=429)
            raise AssertionError(mode)
        finally:
            self.in_flight -= 1


@pytest.fixture
def node():
    server = FakeNode().start()
    yield server
    server.stop()


def referee(node, **kwargs):
    kwargs.setdefault("wait_s", 0.2)
    return Referee(lambda _node_id: node.port, model=MODEL, **kwargs)


async def test_probe_passes_on_a_healthy_node(node):
    result = await referee(node).probe_once("n")
    assert result.ok and result.status == 200


@pytest.mark.parametrize(
    "mode, fragment",
    [
        ("http404", "HTTP 404: Model not found"),
        ("http403", "HTTP 403: Request blocked: parameter 'x' is not allowed"),
        ("plain404", "HTTP 404: 404 page not found"),
        ("empty", "empty content (finish_reason=length)"),
        ("blank", "empty content"),
        ("truncated", "truncated"),
        ("wrong_model", "is not the canonical"),
        ("garbage", "not a chat completion"),
    ],
)
async def test_probe_failure_reasons(node, mode, fragment):
    node.mode = mode
    result = await referee(node).probe_once("n")
    assert not result.ok
    assert fragment in result.detail


async def test_probe_reports_connection_refused():
    ref = Referee(lambda _n: free_port(), model=MODEL)
    result = await ref.probe_once("n")
    assert not result.ok and result.detail == "connection refused"


async def test_probe_rejects_slow_replies(node):
    result = await referee(node, latency_limit_s=0.01).probe_once("n")
    assert not result.ok
    assert "over the" in result.detail or "no response" in result.detail


async def test_verify_passes_after_the_spaced_probes(node):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    verdict = await referee(node, wait_s=8.0, sleep=fake_sleep).verify("n")
    assert verdict.verified
    assert [p.name for p in verdict.probes] == ["probe_a", "burst_1", "burst_2", "burst_3", "burst_4", "probe_b"]
    assert waits == [8.0]
    assert node.served == 6


async def test_verify_stops_after_a_failed_first_probe(node):
    node.mode = "http404"
    verdict = await referee(node).verify("n")
    assert verdict.verdict == "rejected"
    assert [p.name for p in verdict.probes] == ["probe_a"]
    assert "probe A failed" in verdict.note
    assert "probe_a: FAIL" in verdict.evidence()
    assert node.served == 1


async def test_verify_rejects_a_node_that_fails_under_a_burst(node):
    node.mode = "burst_fails"
    verdict = await referee(node).verify("n")
    assert verdict.verdict == "rejected"
    by_name = {p.name: p.ok for p in verdict.probes}
    assert by_name["probe_a"] and by_name["probe_b"]
    assert sum(not p.ok for p in verdict.probes if p.name.startswith("burst")) >= 1


async def test_verify_rejects_a_node_that_dies_after_the_first_probe(node):
    node.die_after = 1
    verdict = await referee(node).verify("n")
    assert verdict.verdict == "rejected"
    assert verdict.probes[0].ok
    assert not verdict.probes[-1].ok
    assert len(verdict.probes) == 6


async def test_probe_event_conversion(node):
    result = await referee(node).probe_once("n", "probe_a")
    event = result.to_event()
    assert event.name == "probe_a" and event.ok
