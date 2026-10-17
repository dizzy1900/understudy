import asyncio
import time
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from tests.agent_support import ScriptedChat, collect, make_context
from understudy import briefing
from understudy.agents.narrator import Narrator
from understudy.llm import BudgetExceeded
from understudy.server import create_app

HOUR = 3600.0
SINCE = 1_000_000.0
GOOD = (
    "Welcome back. You were away 3 hours. 5 incidents were handled and 3 were verified. "
    "1 false fix was caught and 1 incident needs a person. Spend was 15 cents."
)

VERIFICATION = (
    "INSERT INTO verifications (incident_id, node_id, verdict, probes_json, quality, note, ts) VALUES (?, ?, ?, '[]', NULL, '', ?)"
)
LADDER_EVENT = (
    "INSERT INTO ladder_events (ts, fault_class, from_rung, to_rung, direction, passed, total, mean_cost_usd)"
    " VALUES (?, ?, ?, ?, ?, 5, 5, 0.01)"
)


async def seed(db, since: float) -> None:
    """Five live incidents in the window, plus rows that must stay out of every figure."""
    first = await db.open_incident("r1-01", "probe failed", "low", opened_at=since + 10)
    await db.update_incident(first, status="verified", rung="lightning", cost_usd=0.04, escalations=1, closed_at=since + 600)
    second = await db.open_incident("r1-02", "probe failed", "low", opened_at=since + 20)
    await db.update_incident(second, status="verified", rung="super", cost_usd=0.02, closed_at=since + 1200)
    third = await db.open_incident("r1-03", "probe failed", "low", opened_at=since + 2 * HOUR)
    await db.update_incident(third, status="verified", rung="super", cost_usd=0.10, closed_at=since + 3 * HOUR - 600)
    lost = await db.open_incident("r2-01", "probe failed", "high", opened_at=since + 30)
    await db.update_incident(lost, status="unresolved", rung="super", cost_usd=0.5, closed_at=since + 900)
    await db.open_incident("r2-02", "probe failed", "high", opened_at=since + 3 * HOUR - 10)
    proving = await db.open_incident("pg-01", "trial", "low", zone="proving", opened_at=since + 40)
    await db.update_incident(proving, status="verified", cost_usd=0.9, closed_at=since + 50)
    old = await db.open_incident("r1-04", "probe failed", "low", opened_at=since - 100)
    await db.update_incident(old, status="verified", cost_usd=0.7, closed_at=since - 50)

    await db.execute(VERIFICATION, (first, "r1-01", "rejected", since + 100))
    await db.execute(VERIFICATION, (proving, "pg-01", "rejected", since + 45))
    await db.execute(VERIFICATION, (old, "r1-04", "rejected", since - 60))

    for cached, cost, ts in ((250, 0.10, since + 5), (750, 0.05, since + 7)):
        await db.record_llm_call(role="responder", model_key="super", model="m", prompt_tokens=1000, cached_tokens=cached, cost_usd=cost, ts=ts)
    await db.record_llm_call(role="responder", model_key="super", model="m", prompt_tokens=9000, cost_usd=1.0, ts=since - 10)

    await db.execute(LADDER_EVENT, (since + 50, "config_syntax", "super", "lightning", "promote"))
    await db.execute(LADDER_EVENT, (since + 60, "disk_full", "lightning", "super", "demote"))
    await db.execute(LADDER_EVENT, (since - 5, "stale_endpoint", "super", "lightning", "promote"))

    await db.add_event(since + 6, "tokens", {"node_id": "r1-01", "tokens": 120})
    await db.add_event(since + 7, "tokens", {"node_id": "r1-02", "tokens": 80})
    await db.add_event(since - 7, "tokens", {"node_id": "r1-02", "tokens": 999})


async def stats_for(tmp_path, now: float, seeded: bool = True) -> dict:
    context = await make_context(tmp_path, ScriptedChat())
    try:
        if seeded:
            await seed(context.db, SINCE)
        return await briefing.compute_stats(context.db, SINCE, now)
    finally:
        await context.db.close()


def base_stats(**overrides) -> dict:
    stats = {
        "minutes_away": 180, "incidents_handled": 5, "verified": 3, "still_open": 0, "false_fixes_caught": 1,
        "escalations": 1, "promotions": [], "demotions": [], "unresolved": 1, "tokens_served": 200,
        "cost_total_usd": 0.15, "cost_per_incident_usd": None, "cost_per_incident_first_hour_usd": None,
        "cost_per_incident_last_hour_usd": None, "comparison_available": True, "cache_hit_rate": None,
    }
    stats.update(overrides)
    return stats


@pytest.fixture
def served(tmp_path):
    orch = SimpleNamespace(promoter=None)
    chat = ScriptedChat()
    with TestClient(create_app(orch, web_dist=None)) as client:
        ctx = client.portal.call(make_context, tmp_path, chat)
        ctx.narrator = Narrator(ctx)
        orch.ctx, orch.db, orch.bus = ctx, ctx.db, ctx.bus
        yield client, orch, chat
        client.portal.call(ctx.db.close)


# stats ------------------------------------------------------------------------------


def test_stats_read_the_window_from_the_database(tmp_path):
    stats = asyncio.run(stats_for(tmp_path, SINCE + 3 * HOUR))
    assert stats["minutes_away"] == 180
    assert stats["incidents_handled"] == 5 and stats["verified"] == 3
    assert stats["still_open"] == 1 and stats["unresolved"] == 1
    assert stats["escalations"] == 1 and stats["false_fixes_caught"] == 1
    assert stats["promotions"] == [{"fault_class": "config_syntax", "from_rung": "super", "to_rung": "lightning"}]
    assert stats["demotions"] == [{"fault_class": "disk_full", "from_rung": "lightning", "to_rung": "super"}]
    assert stats["tokens_served"] == 200
    assert stats["cost_total_usd"] == pytest.approx(0.15)
    assert stats["cache_hit_rate"] == pytest.approx(0.5)
    assert stats["comparison_available"] is True
    assert stats["cost_per_incident_first_hour_usd"] == pytest.approx(0.03)
    assert stats["cost_per_incident_last_hour_usd"] == pytest.approx(0.10)
    assert stats["cost_per_incident_usd"] == pytest.approx(0.16 / 3, abs=1e-4)


def test_short_window_does_not_offer_a_comparison(tmp_path):
    stats = asyncio.run(stats_for(tmp_path, SINCE + 1500))
    assert stats["comparison_available"] is False and stats["minutes_away"] == 25
    assert stats["incidents_handled"] == 3 and stats["verified"] == 2
    assert stats["cost_per_incident_first_hour_usd"] == pytest.approx(0.03)
    assert stats["cost_per_incident_last_hour_usd"] == pytest.approx(0.03)
    facts = briefing.facts_from(stats)
    assert facts["compare_hours"] is False
    assert "cost_per_incident_first_hour" not in facts and facts["cost_per_incident"] == "3 cents"


def test_empty_window_has_null_costs(tmp_path):
    stats = asyncio.run(stats_for(tmp_path, SINCE + 3 * HOUR, seeded=False))
    assert stats["incidents_handled"] == 0 and stats["tokens_served"] == 0 and stats["cost_total_usd"] == 0
    assert stats["cost_per_incident_first_hour_usd"] is None and stats["cost_per_incident_last_hour_usd"] is None
    assert stats["cost_per_incident_usd"] is None and stats["cache_hit_rate"] is None
    assert stats["promotions"] == [] and stats["demotions"] == []


# facts, validation, fallback --------------------------------------------------------


def test_facts_word_money_and_classes_for_speaking():
    facts = briefing.facts_from(base_stats(
        promotions=[{"fault_class": "config_syntax", "from_rung": "super", "to_rung": "lightning"}],
        cost_total_usd=2.256, minutes_away=75, tokens_served=107_000, cache_hit_rate=0.534,
        cost_per_incident_first_hour_usd=0.03, cost_per_incident_last_hour_usd=0.019,
    ))
    assert facts["time_away"] == "1 hour 15 minutes"
    assert facts["promotions"] == 1 and facts["demotions"] == 0
    assert facts["latest_promotions"] == ["config syntax moved from super to lightning"] and "latest_demotions" not in facts
    assert facts["cost_total"] == "2.26 dollars"
    assert facts["cost_per_incident_first_hour"] == "3 cents" and facts["cost_per_incident_last_hour"] == "1.9 cents"
    assert facts["tokens_served"] == "107,000" and facts["cache_hit_rate"] == "53 percent"


def test_validate_accepts_text_built_from_the_facts():
    assert briefing.validate(GOOD, briefing.facts_from(base_stats())) is None


@pytest.mark.parametrize(
    "text, reason",
    [
        (GOOD.replace("Welcome back.", "Hello again."), "does not start"),
        (" " + GOOD, "does not start"),
        ("Welcome back. " + " ".join(["word"] * 70), "words"),
        (GOOD.replace("5 incidents", "6 incidents"), "number 6"),
        (GOOD.replace("15 cents", "0.15 dollars"), "number 0.15"),
        (GOOD.replace("Spend was 15 cents.", "Spend was $15."), "markup"),
        (GOOD.replace("1 false fix was", "**1 false fix** was"), "markup"),
        (GOOD.replace(". 5 incidents", ".\n5 incidents"), "markup"),
        (GOOD.replace("1 incident needs", "seven incidents need"), "spelled number"),
        (GOOD.replace("Spend was 15 cents.", "Spend was 15 cents, up from twelve."), "spelled number"),
    ],
)
def test_validate_rejects_bad_text(text, reason):
    problem = briefing.validate(text, briefing.facts_from(base_stats()))
    assert problem is not None and reason in problem


def test_validate_reads_numbers_with_thousands_separators():
    facts = briefing.facts_from(base_stats(tokens_served=107_000))
    assert briefing.validate(GOOD + " 107,000 tokens were served.", facts) is None
    assert briefing.validate(GOOD + " 107 tokens were served.", facts) is not None


def test_fallback_starts_with_greeting_and_validates():
    facts = briefing.facts_from(base_stats())
    text = briefing.fallback_text(facts)
    assert text.startswith("Welcome back.") and len(text.split()) <= 70
    assert "1 incident is unresolved and need a person" in text
    assert briefing.validate(text, facts) is None


def test_fallback_stays_within_the_limit_for_the_busiest_window():
    many = [{"fault_class": f"very_long_fault_class_name_{i}", "from_rung": "lightning", "to_rung": "script"} for i in range(9)]
    facts = briefing.facts_from(base_stats(
        promotions=many, demotions=many, minutes_away=1500, incidents_handled=120, verified=100, unresolved=9,
        cost_per_incident_first_hour_usd=0.03, cost_per_incident_last_hour_usd=0.019, cache_hit_rate=0.5, escalations=14,
    ))
    text = briefing.fallback_text(facts)
    assert text.startswith("Welcome back.") and len(text.split()) <= 70
    assert "9 incidents are unresolved and need a person" in text


def test_fallback_names_the_latest_change():
    change = {"fault_class": "disk_full", "from_rung": "super", "to_rung": "lightning"}
    facts = briefing.facts_from(base_stats(promotions=[change, {**change, "fault_class": "bad_key_env"}]))
    text = briefing.fallback_text(facts)
    assert "2 promotions, latest: bad key env moved from super to lightning." in text
    assert briefing.validate(text, facts) is None


def test_fallback_says_plainly_when_nothing_happened():
    facts = briefing.facts_from(base_stats(
        incidents_handled=0, verified=0, unresolved=0, false_fixes_caught=0, escalations=0, tokens_served=0, cost_total_usd=0.0,
    ))
    text = briefing.fallback_text(facts)
    assert text.startswith("Welcome back.") and "no incidents opened and nothing needed fixing" in text
    assert len(text.split()) <= 70


# model path -------------------------------------------------------------------------


async def speak(tmp_path, scripted, **context_overrides):
    context = await make_context(tmp_path, scripted)
    for name, value in context_overrides.items():
        setattr(context, name, value)
    try:
        return await briefing.spoken_text(context, briefing.facts_from(base_stats()))
    finally:
        await context.db.close()


def test_spoken_text_uses_the_model_reply_when_it_validates(tmp_path):
    chat = ScriptedChat(briefing=[GOOD])
    assert asyncio.run(speak(tmp_path, chat)) == (GOOD, "model")
    call = chat.of("briefing")[0]
    assert call["model_key"] == "super" and call["thinking"] is False
    assert call["messages"][0]["content"].startswith("You write a spoken briefing")


def test_spoken_text_retries_once_with_the_reason(tmp_path):
    chat = ScriptedChat(briefing=["Welcome back. 41 incidents were handled.", GOOD])
    assert asyncio.run(speak(tmp_path, chat)) == (GOOD, "model")
    retry = chat.of("briefing")[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "Welcome back. 41 incidents were handled."}
    assert "number 41" in retry[-1]["content"]


@pytest.mark.parametrize(
    "reply",
    [
        "Here you go: Welcome back. Nothing much.",
        "Welcome back. 41 incidents were handled.",
        BudgetExceeded(20.0, 20.0),
        RuntimeError("503"),
    ],
)
def test_spoken_text_falls_back_on_any_failure(tmp_path, reply):
    text, source = asyncio.run(speak(tmp_path, ScriptedChat(briefing=[reply])))
    assert source != "model" and text.startswith("Welcome back.") and len(text.split()) <= 70


def test_spoken_text_falls_back_when_the_model_is_slow(tmp_path, monkeypatch):
    monkeypatch.setattr(briefing, "MODEL_TIMEOUT_S", 0.05)

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    text, source = asyncio.run(speak(tmp_path, ScriptedChat(), chat=slow))
    assert "longer than" in source and text.startswith("Welcome back.")


# endpoints --------------------------------------------------------------------------


def test_away_stores_the_time_and_back_returns_text_and_stats(served):
    client, orch, chat = served
    sent = client.post("/api/away").json()["away_since"]
    assert sent == pytest.approx(time.time(), abs=5)
    assert client.portal.call(orch.db.get_away_since) == pytest.approx(sent)

    since = time.time() - 3 * HOUR
    client.portal.call(orch.db.set_away_since, since)
    client.portal.call(seed, orch.db, since)
    chat.queues["briefing"] = [GOOD]
    seen = client.portal.call(lambda: collect(orch.bus))
    reply = client.post("/api/back")

    body = reply.json()
    assert reply.status_code == 200 and set(body) == {"text", "stats"}
    assert body["text"] == GOOD
    stats = body["stats"]
    assert stats["away_since"] == pytest.approx(since) and stats["incidents_handled"] == 5 and stats["verified"] == 3
    assert stats["cost_total_usd"] == pytest.approx(0.15) and stats["tokens_served"] == 200
    assert client.portal.call(orch.db.get_away_since) is None

    narrations = [e.data for e in client.portal.call(seen) if e.type == "narration"]
    assert [(n["text"], n["priority"]) for n in narrations] == [(GOOD, "high")]
    assert [c["role"] for c in chat.calls] == ["briefing"]


def test_back_uses_the_template_when_the_model_fails(served):
    client, orch, chat = served
    since = time.time() - 3 * HOUR
    client.portal.call(orch.db.set_away_since, since)
    client.portal.call(seed, orch.db, since)
    chat.queues["briefing"] = [RuntimeError("503")]
    body = client.post("/api/back").json()
    assert body["text"].startswith("Welcome back.") and len(body["text"].split()) <= 70
    assert body["stats"]["incidents_handled"] == 5


def test_back_with_nothing_to_report(served):
    client, orch, chat = served
    client.portal.call(orch.db.set_away_since, time.time() - 600)
    chat.queues["briefing"] = [BudgetExceeded(20.0, 20.0)]
    body = client.post("/api/back").json()
    stats = body["stats"]
    assert stats["incidents_handled"] == 0 and stats["comparison_available"] is False
    assert stats["cost_per_incident_first_hour_usd"] is None and stats["minutes_away"] in (9, 10)
    assert "no incidents opened and nothing needed fixing" in body["text"]


def test_back_without_away_since_uses_the_ledger_start(served):
    client, orch, chat = served
    start = time.time() - 2 * HOUR
    client.portal.call(orch.db.set_setting, "ledger_since", repr(start))
    chat.queues["briefing"] = [RuntimeError("503")]
    stats = client.post("/api/back").json()["stats"]
    assert stats["away_since"] == pytest.approx(start) and stats["comparison_available"] is True
