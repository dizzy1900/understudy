import asyncio

import pytest

from config import settings
from tests.fleet_support import MODEL, FakeNode
from understudy.db import Database
from understudy.events import EventBus
from understudy.fleet.monitor import Monitor
from understudy.fleet.referee import Referee
from understudy.fleet.state import FleetState
from understudy.fleet.traffic import Traffic

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def node():
    server = FakeNode().start()
    yield server
    server.stop()


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(tmp_path / "t.db")
    yield database
    await database.close()


def spec(node_id="n-01", zone="live", row=1, col=1, port=1):
    return settings.NodeSpec(node_id, row, col, zone, port)


def drain(sub):
    items = []
    while not sub._queue.empty():
        items.append(sub._queue.get_nowait())
    return items


def test_state_publishes_status_only_on_change_and_windows_tokens():
    clock = Clock()
    bus = EventBus()
    sub = bus.subscribe()
    state = FleetState([spec()], bus, clock)
    assert not state.set_status("n-01", "healthy")
    assert state.set_status("n-01", "degraded", "slow")
    assert not state.set_status("n-01", "degraded", "still slow")
    events = drain(sub)
    assert [(e.type, e.data["status"]) for e in events] == [("node_status", "degraded")]

    state.record_tokens("n-01", 30)
    clock.now += 40
    state.record_tokens("n-01", 20)
    assert state.tokens_per_min("n-01") == 50
    clock.now += 30
    assert state.tokens_per_min("n-01") == 20
    assert state.nodes["n-01"].tokens_total == 50


def test_default_snapshot_covers_the_whole_layout():
    snap = FleetState().snapshot()
    assert len(snap.nodes) == 16
    assert sum(n.zone == "live" for n in snap.nodes) == 12
    assert {n.id for n in snap.nodes if n.zone == "proving"} == {"pg-01", "pg-02", "pg-03", "pg-04"}
    assert snap.nodes[0].id == "r1-01" and snap.nodes[0].status == "healthy"


class StubSupervisor:
    def __init__(self, state, exit_code):
        self._status = type("S", (), {"state": state, "exit_code": exit_code})()

    def status(self, node_id):
        return self._status


async def test_two_consecutive_failures_open_one_incident(node, db):
    bus = EventBus()
    sub = bus.subscribe()
    state = FleetState([spec()], bus)
    referee = Referee(lambda _n: node.port, model=MODEL)
    monitor = Monitor(state, referee, db, bus)
    node.mode = "http404"

    await monitor.check("n-01")
    assert state.status("n-01") == "degraded"
    assert await db.open_incidents() == []

    await monitor.check("n-01")
    assert state.status("n-01") == "down"
    incidents = await db.open_incidents()
    assert len(incidents) == 1
    assert incidents[0]["node_id"] == "n-01" and incidents[0]["severity"] == "low"
    assert "HTTP 404" in incidents[0]["symptom"]

    await monitor.check("n-01")
    assert len(await db.open_incidents()) == 1

    types = [e.type for e in drain(sub)]
    assert types.count("incident_opened") == 1
    assert types == ["node_status", "node_status", "incident_opened"]


async def test_one_failure_then_recovery_opens_nothing(node, db):
    state = FleetState([spec()])
    monitor = Monitor(state, Referee(lambda _n: node.port, model=MODEL), db)
    node.mode = "http403"
    await monitor.check("n-01")
    node.mode = "ok"
    await monitor.check("n-01")
    assert state.status("n-01") == "healthy"
    assert monitor.failures["n-01"] == 0
    assert await db.open_incidents() == []


async def test_unreachable_node_is_high_severity_and_reports_exit_code(db):
    state = FleetState([spec()])
    referee = Referee(lambda _n: 1, model=MODEL)
    monitor = Monitor(state, referee, db, supervisor=StubSupervisor("exited", 4))
    await monitor.check("n-01")
    await monitor.check("n-01")
    incident = (await db.open_incidents())[0]
    assert incident["severity"] == "high"
    assert "connection refused" in incident["symptom"] and "code 4" in incident["symptom"]


async def test_repairing_node_is_left_alone_and_incident_links_ground_truth(node, db):
    state = FleetState([spec()])
    monitor = Monitor(state, Referee(lambda _n: node.port, model=MODEL), db)
    await db.add_fault_truth("n-01", "model_id_case", 5, {"x": 1})
    node.mode = "http404"
    await monitor.check("n-01")
    await monitor.check("n-01")
    incident_id = (await db.open_incidents())[0]["id"]
    truth = await db.active_faults("n-01")
    assert truth[0]["incident_id"] == incident_id

    state.mark_repairing("n-01")
    for _ in range(3):
        await monitor.check("n-01")
    assert state.status("n-01") == "repairing"
    assert len(await db.open_incidents()) == 1

    await db.clear_fault_truth("n-01")
    assert await db.active_faults("n-01") == []


async def test_monitor_loop_probes_live_nodes_only(node, db):
    specs = [spec("n-01", port=node.port), spec("pg-01", "proving", col=2, port=node.port)]
    state = FleetState(specs)
    monitor = Monitor(state, Referee(lambda _n: node.port, model=MODEL), db, interval_s=0.1)
    await monitor.start()
    await asyncio.sleep(0.5)
    await monitor.stop()
    assert node.served >= 2
    assert state.nodes["n-01"].last_probe_ms > 0
    assert state.nodes["pg-01"].last_probe_ms == 0


async def test_traffic_publishes_tokens_for_healthy_live_nodes_only(node):
    bus = EventBus()
    sub = bus.subscribe()
    specs = [
        spec("n-01", port=node.port),
        spec("n-02", col=2, port=node.port),
        spec("pg-01", "proving", col=3, port=node.port),
    ]
    state = FleetState(specs, bus)
    state.set_status("n-02", "down")
    drain(sub)
    traffic = Traffic(state, lambda _n: node.port, bus, interval_s=0.1, snapshot_interval_s=0.15)
    await traffic.start()
    await asyncio.sleep(0.6)
    await traffic.stop()
    events = drain(sub)
    token_nodes = {e.data["node_id"] for e in events if e.type == "tokens"}
    assert token_nodes == {"n-01"}
    assert all(e.data["tokens"] == 12 for e in events if e.type == "tokens")
    snapshots = [e for e in events if e.type == "fleet_snapshot"]
    assert len(snapshots) >= 2
    by_id = {n["id"]: n for n in snapshots[-1].data["nodes"]}
    assert by_id["n-01"]["tokens_per_min"] > 0
    assert by_id["n-02"]["status"] == "down" and by_id["n-02"]["tokens_per_min"] == 0
    assert state.nodes["n-01"].tokens_total == sum(e.data["tokens"] for e in events if e.type == "tokens")


async def test_traffic_failures_are_counted_not_published(node):
    bus = EventBus()
    sub = bus.subscribe()
    state = FleetState([spec("n-01", port=node.port)], bus)
    node.mode = "http404"
    traffic = Traffic(state, lambda _n: node.port, bus)
    await traffic.start()
    try:
        assert await traffic.send_one("n-01") is None
    finally:
        await traffic.stop()
    assert traffic.failed == 1
    assert all(e.type != "tokens" for e in drain(sub))


async def test_hold_keeps_a_failing_node_from_reopening_an_incident(node, db):
    state = FleetState([spec()])
    monitor = Monitor(state, Referee(lambda _n: node.port, model=MODEL), db)
    node.mode = "http404"
    monitor.hold("n-01", 60)
    for _ in range(4):
        await monitor.check("n-01")
    assert state.status("n-01") == "down"
    assert await db.open_incidents() == []
    monitor._held["n-01"] = 0.0
    await monitor.check("n-01")
    assert len(await db.open_incidents()) == 1
