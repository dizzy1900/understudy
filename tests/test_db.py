import pytest

from config import settings
from understudy.db import Database

TABLES = {
    "incidents",
    "actions",
    "verifications",
    "llm_calls",
    "skills",
    "ladder",
    "ladder_events",
    "heldout_reports",
    "faults_truth",
    "events",
    "settings",
}


async def open_db(tmp_path):
    return await Database.open(tmp_path / "test.db")


@pytest.mark.anyio
async def test_every_table_exists_and_journal_is_wal(tmp_path):
    db = await open_db(tmp_path)
    tables = {row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert TABLES <= tables
    assert await db.scalar("PRAGMA journal_mode") == "wal"
    await db.close()


@pytest.mark.anyio
async def test_ladder_starts_at_super_and_survives_reopen(tmp_path):
    db = await open_db(tmp_path)
    assert await db.get_ladder() == {name: "super" for name in settings.TRAINING_FAULT_CLASSES}
    await db.set_ladder("disk_full", "lightning")
    await db.close()

    db = await open_db(tmp_path)
    assert (await db.get_ladder())["disk_full"] == "lightning"
    await db.close()


@pytest.mark.anyio
async def test_settings_and_away_since(tmp_path):
    db = await open_db(tmp_path)
    assert await db.get_setting("missing") is None
    assert await db.get_setting("missing", "x") == "x"
    await db.set_setting("k", "1")
    await db.set_setting("k", "2")
    assert await db.get_setting("k") == "2"

    assert await db.get_away_since() is None
    await db.set_away_since(1792263000.123456)
    assert await db.get_away_since() == 1792263000.123456
    await db.set_away_since(None)
    assert await db.get_away_since() is None
    await db.close()


@pytest.mark.anyio
async def test_llm_call_totals(tmp_path):
    db = await open_db(tmp_path)
    await db.record_llm_call(role="a", model_key="super", model="m", prompt_tokens=100, cached_tokens=40, cost_usd=0.25)
    await db.record_llm_call(role="b", model_key="nano", model="n", prompt_tokens=50, cost_usd=0.5, incident_id=3,
                             agent_id="resp-1", meta={"why": "x"})
    await db.record_llm_call(role="c", model_key="nano", model="n", ok=False, error="boom")
    assert await db.total_cost() == pytest.approx(0.75)
    assert await db.cache_stats() == (150, 40)
    row = await db.fetchone("SELECT * FROM llm_calls WHERE role = 'b'")
    assert (row["incident_id"], row["agent_id"], row["meta_json"]) == (3, "resp-1", '{"why":"x"}')
    await db.close()


@pytest.mark.anyio
async def test_incident_lifecycle(tmp_path):
    db = await open_db(tmp_path)
    incident = await db.open_incident("r1-01", "404", "low", opened_at=10.0)
    assert [row["id"] for row in await db.open_incidents()] == [incident]

    await db.add_action(incident, "resp-1", 1, "tail_log", {"n": 40}, True, "ok")
    await db.add_verification(incident, "r1-01", "verified", [{"name": "probe_a", "ok": True}], quality=5)
    await db.update_incident(incident, status="verified", rung="lightning", steps=4, cost_usd=0.01, closed_at=20.0)

    row = await db.get_incident(incident)
    assert (row["status"], row["rung"], row["steps"], row["closed_at"]) == ("verified", "lightning", 4, 20.0)
    assert await db.open_incidents() == []
    assert [a["tool"] for a in await db.actions_for(incident)] == ["tail_log"]

    with pytest.raises(ValueError):
        await db.update_incident(incident, node_id="r2-02")
    await db.close()


@pytest.mark.anyio
async def test_skills_are_versioned_per_class(tmp_path):
    db = await open_db(tmp_path)
    assert await db.save_skill("disk_full", {"fix": "a"}, "first") == 1
    assert await db.save_skill("disk_full", {"fix": "b"}, "second") == 2
    assert await db.save_skill("bad_key_env", {"fix": "c"}) == 1

    latest = {row["fault_class"]: row for row in await db.latest_skills()}
    assert latest["disk_full"]["version"] == 2
    assert latest["disk_full"]["card"] == {"fix": "b"}
    assert latest["bad_key_env"]["version"] == 1
    await db.close()


@pytest.mark.anyio
async def test_events_and_evaluation_tables(tmp_path):
    db = await open_db(tmp_path)
    await db.add_event(1.0, "tokens", {"node_id": "r1-01", "tokens": 5})
    await db.add_event(2.0, "narration", {"text": "hi"})
    events = await db.events_since(1.0)
    assert [(e["type"], e["data"]) for e in events] == [("narration", {"text": "hi"})]

    await db.add_fault_truth("r1-01", "disk_full", 7, {"fill_mb": 12})
    await db.add_ladder_event("disk_full", "super", "lightning", "promote", 5, 5, 0.004)
    await db.add_heldout_report("unseen_instance", "disk_full", "lightning", True, seed=100001, node_id="pg-01")
    assert await db.scalar("SELECT COUNT(*) FROM faults_truth") == 1
    assert await db.scalar("SELECT COUNT(*) FROM ladder_events") == 1
    assert await db.scalar("SELECT passed FROM heldout_reports") == 1
    await db.close()
