import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from tests.agent_support import NODE, ScriptedChat, make_context
from understudy import briefing, ledger
from understudy.agents import chaos as chaos_module
from understudy.agents.chaos import ChaosOps
from understudy.events import Narration
from understudy.server import create_app


@pytest.fixture
def served(tmp_path):
    orch = SimpleNamespace(promoter=None)
    with TestClient(create_app(orch, web_dist=None)) as client:
        ctx = client.portal.call(make_context, tmp_path, ScriptedChat())
        orch.ctx, orch.db, orch.bus, orch.state = ctx, ctx.db, ctx.bus, ctx.state
        orch.chaos = ChaosOps(ctx)
        yield client, orch
        client.portal.call(ctx.db.close)


def seed(client, orch):
    db = orch.db

    async def fill():
        await ledger.ledger_since(db)
        since = await ledger.ledger_since(db)
        first = await db.open_incident("r1-01", "probe failed", "low", opened_at=since + 1)
        await db.update_incident(first, status="verified", rung="super", cost_usd=0.04, closed_at=since + 20, fault_class="disk_full")
        second = await db.open_incident("r1-02", "probe failed", "low", opened_at=since + 2)
        await db.update_incident(second, status="verified", rung="lightning", cost_usd=0.02, closed_at=since + 30)
        await db.open_incident("r1-03", "probe failed", "high", opened_at=since + 3)
        await db.add_verification(first, "r1-01", "rejected", [], None, "probe A failed")
        await db.add_verification(first, "r1-01", "verified", [], 5, "ok")
        await db.record_llm_call(role="responder", model_key="super", model="m", prompt_tokens=1000, cached_tokens=250, cost_usd=0.1, ts=since + 5)
        await db.add_ladder_event("disk_full", "super", "lightning", "promote", 5, 5, 0.01)
        await db.add_event(since + 6, "tokens", {"node_id": "r1-01", "tokens": 120})
        await db.add_event(since + 7, "tokens", {"node_id": "r1-02", "tokens": 80})
        return since

    return client.portal.call(fill)


def test_ledger_numbers_come_from_the_database(served):
    client, orch = served
    seed(client, orch)
    body = client.get("/api/ledger").json()
    assert body["incidents_total"] == 3 and body["verified"] == 2 and body["rejected_claims"] == 1
    assert body["promotions"] == 1 and body["demotions"] == 0
    assert body["cost_total_usd"] == pytest.approx(0.1)
    assert body["cost_per_incident_usd"] == pytest.approx(0.03)
    assert body["tokens_served"] == 200 and body["cache_hit_rate"] == pytest.approx(0.25)
    assert body["by_rung"]["super"] == {"incidents": 1, "cost_usd": 0.04}
    assert body["by_rung"]["lightning"]["incidents"] == 1 and body["by_rung"]["script"]["incidents"] == 0
    assert body["ladder"]["disk_full"] == "super" and len(body["ladder"]) == 10


def test_ladder_skills_and_history(served):
    client, orch = served
    seed(client, orch)
    client.portal.call(orch.db.save_skill, "disk_full", {"signature": "507", "fix_steps": ["delete spool"]}, "first card")
    ladder = client.get("/api/ladder").json()
    assert ladder["events"][-1]["evidence"] == {"passed": 5, "total": 5, "mean_cost_usd": 0.01}
    skills = client.get("/api/skills").json()
    assert skills[0]["fault_class"] == "disk_full" and skills[0]["version"] == 1 and skills[0]["signature"] == "507"
    events = client.get("/api/history").json()["events"]
    assert [e["type"] for e in events] == ["tokens", "tokens"]
    body = client.get("/api/history").json()
    assert [e["evidence"]["total"] for e in body["ladder_events"]] == [5]
    assert [(p["total"], p["verified"]) for p in body["incidents"]] == [(1, 0), (2, 0), (3, 0), (3, 1), (3, 2)]
    assert [p["value"] for p in body["cost_per_incident"]] == pytest.approx([0.04, 0.03])
    assert client.get("/api/history", params={"since": 1e12}).json() == {
        "events": [], "ladder_events": [], "incidents": [], "cost_per_incident": []}


def test_websocket_starts_with_snapshot_and_ledger_then_streams(served):
    client, orch = served
    with client.websocket_connect("/ws") as sock:
        assert sock.receive_json()["type"] == "fleet_snapshot"
        assert sock.receive_json()["type"] == "ledger"
        client.portal.call(_publish, orch.bus, Narration(text="hello", priority="low"))
        message = sock.receive_json()
        assert message["type"] == "narration" and message["data"]["text"] == "hello"


async def _publish(bus, payload):
    bus.publish(payload)


def test_inject_validation(served, monkeypatch):
    client, orch = served
    assert client.post("/api/inject", json={"node_id": "r9-09", "fault_class": "disk_full"}).status_code == 404
    assert client.post("/api/inject", json={"node_id": NODE, "fault_class": "gremlins"}).status_code == 422

    seen = []

    async def fake_inject(sup, node_id, fault_class, seed=None, db=None):
        seen.append((node_id, fault_class))
        return SimpleNamespace(seed=seed, node_id=node_id, fault_class=fault_class)

    monkeypatch.setattr(chaos_module.faults, "inject", fake_inject)
    ok = client.post("/api/inject", json={"node_id": NODE, "fault_class": "disk_full"})
    assert ok.status_code == 200 and ok.json() == {"injected": [{"node_id": NODE, "fault_class": "disk_full"}]}
    assert seen == [(NODE, "disk_full")]
    orch.state.set_status(NODE, "down")
    assert client.post("/api/inject", json={"node_id": NODE, "fault_class": "disk_full"}).status_code == 409


def test_chaos_command_returns_the_plan_and_confirmation(served, monkeypatch):
    client, orch = served

    async def fake_inject(sup, node_id, fault_class, seed=None, db=None):
        return SimpleNamespace(seed=seed, node_id=node_id, fault_class=fault_class)

    monkeypatch.setattr(chaos_module.faults, "inject", fake_inject)
    orch.ctx.chat.queues["chaos"] = [
        '{"action": "inject", "rows": null, "targets": [{"node_id": "r1-01", "fault_class": "disk_full"}]}',
        '{"action": null, "rows": null, "targets": []}',
    ]
    ok = client.post("/api/chaos", json={"command": "fill the disk on r1-01"})
    assert ok.status_code == 200
    assert ok.json() == {
        "injected": [{"node_id": NODE, "fault_class": "disk_full"}],
        "plan": {"action": "inject", "rows": None, "targets": [{"node_id": NODE, "fault_class": "disk_full"}]},
        "text": "Injected disk full on r1-01.",
    }
    nonsense = client.post("/api/chaos", json={"command": "what is the weather"})
    assert nonsense.status_code == 422 and "could not understand" in nonsense.json()["detail"]


def test_storm_without_free_nodes_is_a_409(served):
    client, orch = served
    orch.state.set_status(NODE, "down")
    reply = client.post("/api/storm", json={})
    assert reply.status_code == 409 and reply.json()["detail"] == "no free healthy nodes"


def test_away_and_back_report_the_window_from_the_database(served, monkeypatch):
    client, orch = served
    since = seed(client, orch)
    sent = client.post("/api/away").json()["away_since"]
    assert sent == pytest.approx(time.time(), abs=5)
    client.portal.call(orch.db.set_away_since, since)
    monkeypatch.setattr(briefing, "time", SimpleNamespace(time=lambda: since + 100))
    back = client.post("/api/back").json()
    assert set(back) == {"text", "stats"}
    assert back["text"].startswith("Welcome back.") and len(back["text"].split()) <= 70
    stats = back["stats"]
    assert stats["away_since"] == pytest.approx(since)
    assert stats["incidents_handled"] == 3 and stats["verified"] == 2 and stats["false_fixes_caught"] == 1
    assert [p["fault_class"] for p in stats["promotions"]] == ["disk_full"] and stats["demotions"] == []
    assert stats["tokens_served"] == 200 and stats["cost_total_usd"] == pytest.approx(0.1)
    assert stats["cache_hit_rate"] == pytest.approx(0.25)
    assert client.portal.call(orch.db.get_away_since) is None


def test_stubs_answer_plainly(served):
    client, _ = served
    assert client.get("/api/voice/status").json()["available"] is False
    assert client.post("/api/voice", content=b"x").status_code == 422
    assert client.post("/api/promote/run").json()["started"] is False
    assert client.post("/api/demo/reset").status_code == 403


def test_cors_allows_the_dev_server(served):
    client, _ = served
    reply = client.get("/api/ledger", headers={"Origin": "http://localhost:5173"})
    assert reply.headers["access-control-allow-origin"] == "http://localhost:5173"
