import time

import pytest
from starlette.testclient import TestClient

from scripts.mock_events import FAULTS, create_app
from understudy import events as ev


@pytest.fixture
def client():
    with TestClient(create_app(speed=60)) as client:
        yield client


def test_every_event_type_flows_and_validates(client):
    seen: set[str] = set()
    verdicts: set[str] = set()
    deadline = time.monotonic() + 40
    with client.websocket_connect("/ws") as sock:
        first = ev.Envelope.model_validate_json(sock.receive_text())
        assert first.type == "fleet_snapshot"
        nodes = first.payload().nodes
        assert [n.zone for n in nodes].count("live") == 12
        assert [n.zone for n in nodes].count("proving") == 4

        while seen != set(ev.EVENT_TYPES) and time.monotonic() < deadline:
            envelope = ev.Envelope.model_validate_json(sock.receive_text())
            payload = envelope.payload()
            seen.add(envelope.type)
            if envelope.type == "verification":
                verdicts.add(payload.verdict)
    assert seen == set(ev.EVENT_TYPES)
    assert verdicts == {"verified", "rejected"}


def test_rest_stubs_return_plausible_shapes(client):
    assert set(client.get("/api/ledger").json()) == set(ev.Ledger.model_fields)
    ladder = client.get("/api/ladder").json()
    assert set(ladder["ladder"].values()) <= {"super", "lightning", "script"}
    assert isinstance(client.get("/api/skills").json(), list)
    assert "events" in client.get("/api/history", params={"since": 0}).json()
    assert client.get("/api/voice/status").json()["available"] is False


def test_operator_endpoints(client):
    injected = client.post("/api/inject", json={"node_id": "r1-01", "fault_class": "disk_full"})
    assert injected.status_code == 200
    assert client.post("/api/inject", json={"node_id": "r1-01", "fault_class": "disk_full"}).status_code == 409
    assert client.post("/api/inject", json={"node_id": "zz-99", "fault_class": "disk_full"}).status_code == 404
    assert client.post("/api/inject", json={"node_id": "r1-02", "fault_class": "nope"}).status_code == 422

    assert client.post("/api/storm", json={"rows": [3]}).status_code == 200
    assert "injected" in client.post("/api/chaos", json={"command": "kill r2-03 with a crashed worker"}).json()
    assert client.post("/api/away").json()["away_since"] > 0
    assert "While you were away" in client.post("/api/back").json()["briefing"]
    assert client.post("/api/promote/run").json()["started"] is True
    assert client.post("/api/demo/reset").json() == {"ok": True}


def test_every_fault_class_has_a_story():
    from config import settings

    for name in (*settings.TRAINING_FAULT_CLASSES, *settings.HELDOUT_FAULT_CLASSES):
        assert FAULTS[name].steps
        assert all(len(thought) <= ev.THOUGHT_MAX for thought, *_ in FAULTS[name].steps)
