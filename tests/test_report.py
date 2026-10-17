import json

import pytest

from scripts import report
from understudy import history
from understudy.db import Database

pytestmark = pytest.mark.anyio

T0 = 1_000_000.0
MIN = 60.0


@pytest.fixture(autouse=True)
async def close_databases():
    opened = []
    original = Database.open.__func__

    async def tracking(cls, path=None):
        db = await original(cls, path)
        opened.append(db)
        return db

    Database.open = classmethod(tracking)
    yield
    Database.open = classmethod(original)
    for db in opened:
        try:
            await db.close()
        except ValueError:
            pass


async def incident(db, opened, closed, cls, status, cost, rung="super"):
    iid = await db.open_incident("r1-01", "probe failed", "low", opened_at=T0 + opened * MIN)
    await db.update_incident(iid, fault_class=cls, status=status, cost_usd=cost, rung=rung,
                             closed_at=None if closed is None else T0 + closed * MIN)
    return iid


async def seed(db):
    await incident(db, 0, 5, "disk_full", "verified", 0.04)
    await incident(db, 1, 8, "disk_full", "unresolved", 0.06)
    await incident(db, 10, 20, "bad_key_env", "verified", 0.02)
    await incident(db, 30, 40, "disk_full", "verified", 0.02, rung="lightning")
    await incident(db, 35, 50, "bad_key_env", "verified", 0.04)
    await incident(db, 55, None, "disk_full", "open", 0.0)
    await db.record_llm_call(role="responder", model_key="super", model="m", prompt_tokens=1000,
                             cached_tokens=250, cost_usd=0.17, ts=T0 + MIN)
    await db.record_llm_call(role="triage", model_key="nano", model="n", prompt_tokens=1000,
                             cached_tokens=250, cost_usd=0.03, ts=T0 + MIN)
    await db.execute(
        "INSERT INTO ladder_events (ts, fault_class, from_rung, to_rung, direction, passed, total, mean_cost_usd)"
        " VALUES (?, 'disk_full', 'super', 'lightning', 'promote', 5, 5, 0.01)", (T0 + 25 * MIN,))
    for seed_, ok in ((100001, 1), (100002, 1), (100003, 0)):
        await db.add_heldout_report("proving", "disk_full", "lightning", bool(ok), seed=seed_, steps=6, cost_usd=0.01)
    await db.add_heldout_report("heldout_class", "log_permission", "lightning", False, seed=100010,
                                steps=14, cost_usd=0.02, detail={"reason": "step budget", "escalated": True})
    await db.add_event(T0 + MIN, "tokens", {"node_id": "r1-01", "tokens": 120})
    await db.add_event(T0 + 2 * MIN, "tokens", {"node_id": "r1-02", "tokens": 80})
    await db.set_setting("away_since", repr(T0 + 32 * MIN))


async def test_report_numbers(tmp_path):
    db = await Database.open(tmp_path / "r.db")
    await seed(db)
    await db.close()
    text = await report.run(tmp_path / "r.db", tmp_path / "r.md", tmp_path / "r.json")
    data = json.loads((tmp_path / "r.json").read_text())
    assert (tmp_path / "r.md").read_text() == text

    assert data["window"]["mode"] == "halves"
    first, last = data["overall"]["first"], data["overall"]["last"]
    assert (first["incidents"], first["verified"]) == (3, 2)
    assert first["success_rate"] == pytest.approx(2 / 3, abs=1e-3) and first["mean_cost"] == pytest.approx(0.04)
    assert (last["incidents"], last["verified"]) == (2, 2) and last["mean_cost"] == pytest.approx(0.03)
    assert data["per_class"]["disk_full"]["overall"]["success_rate"] == pytest.approx(2 / 3, abs=1e-3)

    assert data["overall"]["closed_incidents"] == 5
    assert data["overall"]["llm_cost_per_incident"] == pytest.approx(0.04)
    assert data["overall"]["attributed_cost_per_incident"] == pytest.approx(0.036)
    assert data["cache_hit_rate"] == pytest.approx(0.25)
    assert data["tokens_served"] == 200
    assert data["ladder"]["timeline"]["disk_full"][0]["to_rung"] == "lightning"
    assert data["promotions"][0]["evidence"] == {"passed": 5, "total": 5, "mean_cost_usd": 0.01}
    assert data["proving"]["disk_full"]["lightning"]["passed"] == 2
    assert data["proving"]["disk_full"]["lightning"]["total"] == 3
    run = data["heldout_classes"]["log_permission"]["runs"][0]
    assert run["passed"] is False and run["reason"] == "step budget"
    assert data["generalization"]["unseen_classes"] == {"passed": 0, "total": 1, "rate": 0.0}
    away = data["since_away"]
    assert (away["incidents"], away["verified"], away["open"]) == (2, 1, 1)
    assert away["by_class"]["bad_key_env"]["verified"] == 1
    assert "halves" in text and "FAIL" in text and "step budget" in text


async def test_thirty_minute_windows(tmp_path):
    db = await Database.open(tmp_path / "r.db")
    await incident(db, 0, 10, "disk_full", "verified", 0.10)
    await incident(db, 40, 50, "disk_full", "unresolved", 0.30)
    await incident(db, 70, 90, "disk_full", "verified", 0.02)
    data = await report.collect(db)
    assert data["window"]["mode"] == "30min"
    assert data["overall"]["first"]["incidents"] == 1 and data["overall"]["first"]["mean_cost"] == pytest.approx(0.10)
    assert data["overall"]["last"]["incidents"] == 1 and data["overall"]["last"]["mean_cost"] == pytest.approx(0.02)


async def test_empty_database(tmp_path):
    text = await report.run(tmp_path / "e.db", None, tmp_path / "out" / "e.json")
    data = json.loads((tmp_path / "out" / "e.json").read_text())
    assert data["window"]["start"] is None and data["per_class"] == {}
    assert data["tokens_served"] == 0 and data["cache_hit_rate"] is None
    assert data["since_away"] == {"away_since": None}
    assert "No away period is set." in text and "No ladder events." in text


async def test_history_series(tmp_path):
    db = await Database.open(tmp_path / "h.db")
    await seed(db)
    body = await history.build_history(db, 0.0)
    assert [(p["total"], p["verified"]) for p in body["incidents"]][-1] == (6, 4)
    assert [p["ts"] for p in body["incidents"]] == sorted(p["ts"] for p in body["incidents"])
    assert body["cost_per_incident"][-1]["value"] == pytest.approx(0.036)
    assert body["ladder_events"][0]["direction"] == "promote"


async def test_series_are_thinned(tmp_path):
    db = await Database.open(tmp_path / "t.db")
    for i in range(700):
        await incident(db, i, i + 0.5, "disk_full", "verified", 0.01)
    points = await history.incident_series(db)
    assert len(points) == 500 and points[-1]["total"] == 700 and points[-1]["verified"] == 700
    assert len(await history.cost_per_incident_series(db)) == 500


async def test_history_empty(tmp_path):
    db = await Database.open(tmp_path / "x.db")
    assert await history.build_history(db, 0.0) == {"ladder_events": [], "incidents": [], "cost_per_incident": []}
