import pytest

from tests.fleet_support import MODEL, FakeNode, free_port
from understudy.fleet.referee import Referee

pytestmark = pytest.mark.anyio


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
