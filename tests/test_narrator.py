import asyncio

import pytest

from tests.agent_support import NODE, OPEN, FakeReferee, ScriptedChat, collect, make_context, turn
from understudy import context as context_module
from understudy.agents import narrator as lines
from understudy.agents.commander import Commander
from understudy.agents.narrator import WORD_MAX, Narrator, accept, short_reason, word_count
from understudy.llm import BudgetExceeded

pytestmark = pytest.mark.anyio

FACTS = "r2-03 is healthy again: disk_full fixed at the super rung in 8 steps for $0.011."


@pytest.fixture(autouse=True)
async def close_databases():
    yield
    while OPEN:
        await OPEN.pop().close()


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def setup(tmp_path, *replies, **kwargs):
    chat = ScriptedChat(narrator=list(replies))
    ctx = await make_context(tmp_path, chat)
    clock = FakeClock()
    ctx.narrator = Narrator(ctx, clock=clock, sleep=clock.sleep, **kwargs)
    return ctx, chat, clock, collect(ctx.bus)


def texts(drain, priority=None):
    return [e.data["text"] for e in drain() if e.type == "narration" and priority in (None, e.data["priority"])]


# rate limit ----------------------------------------------------------------------------


async def test_say_returns_the_facts_without_publishing_inline(tmp_path):
    ctx, _, _, drain = await setup(tmp_path)
    assert await ctx.say("Plain facts.", "low", polish=False) == "Plain facts."
    assert texts(drain) == []
    await ctx.narrator.drain()
    assert texts(drain) == ["Plain facts."]


async def test_low_line_inside_the_gap_is_dropped(tmp_path):
    ctx, _, clock, drain = await setup(tmp_path)
    narrator = ctx.narrator
    await narrator.say("first", polish=False)
    await narrator.drain()
    clock.now += 3.9
    await narrator.say("second", polish=False)
    await narrator.drain()
    clock.now += 0.2
    await narrator.say("third", polish=False)
    await narrator.drain()
    assert texts(drain) == ["first", "third"]
    assert clock.sleeps == []


async def test_low_line_is_dropped_while_a_high_line_waits(tmp_path):
    ctx, _, _, drain = await setup(tmp_path)
    narrator = ctx.narrator
    await narrator.say("storm", "high", polish=False)
    await narrator.say("healthy", "low", polish=False)
    await narrator.drain()
    assert texts(drain) == ["storm"]


async def test_high_line_waits_for_the_gap_and_order_is_kept(tmp_path):
    ctx, _, clock, drain = await setup(tmp_path)
    narrator = ctx.narrator
    await narrator.say("low one", polish=False)
    await narrator.drain()
    await narrator.say("high one", "high", polish=False)
    await narrator.say("high two", "high", polish=False)
    await narrator.drain()
    assert texts(drain) == ["low one", "high one", "high two"]
    assert clock.sleeps == [4.0, 4.0]


async def test_overflow_drops_the_queued_low_line_first_then_the_oldest(tmp_path):
    ctx, _, _, drain = await setup(tmp_path)
    narrator = ctx.narrator
    await narrator.say("low", "low", polish=False)
    for name in ("h1", "h2", "h3"):
        await narrator.say(name, "high", polish=False)
    await narrator.say("h4", "high", polish=False)
    await narrator.drain()
    assert texts(drain) == ["h2", "h3", "h4"]


async def test_spawn_events_only_for_lines_that_are_polished_and_published(tmp_path):
    ctx, chat, _, drain = await setup(tmp_path, "r2-03 is healthy again; disk_full fixed in 8 steps for $0.011.")
    narrator = ctx.narrator
    await narrator.say(FACTS, polish=True)
    await narrator.drain()
    await narrator.say(FACTS, polish=True)
    await narrator.say("unpolished", "high", polish=False)
    await narrator.drain()
    events = drain()
    roles = [e.data["role"] for e in events if e.type == "agent_spawned"]
    assert roles == ["narrator"] and len(chat.of("narrator")) == 1
    assert [e.type for e in events].count("agent_done") == 1


async def test_close_cancels_a_waiting_worker(tmp_path):
    ctx, _, clock, drain = await setup(tmp_path)
    narrator = ctx.narrator
    started = asyncio.Event()

    async def hang(seconds):
        started.set()
        await asyncio.Event().wait()

    narrator._sleep = hang
    await narrator.say("one", polish=False)
    await narrator.drain()
    await narrator.say("two", "high", polish=False)
    await started.wait()
    await narrator.close()
    assert narrator._task is None
    assert texts(drain) == ["one"]


async def test_publish_failure_does_not_reach_the_caller_or_stop_later_lines(tmp_path):
    ctx, _, _, drain = await setup(tmp_path)
    real = ctx.bus.publish
    calls = []

    def flaky(event):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError("bus down")
        real(event)

    ctx.bus.publish = flaky
    await ctx.say("one", "high", polish=False)
    await ctx.say("two", "high", polish=False)
    await ctx.narrator.drain()
    assert texts(drain) == ["two"]


# force ---------------------------------------------------------------------------------


async def test_force_publishes_at_once_past_the_gap_and_the_word_cap(tmp_path):
    ctx, _, clock, drain = await setup(tmp_path)
    narrator = ctx.narrator
    await narrator.say("recent", polish=False)
    await narrator.drain()
    long_text = " ".join(f"word{i}" for i in range(60))
    assert await narrator.say(long_text, "high", polish=False, force=True) == long_text
    assert texts(drain) == ["recent", long_text]
    assert clock.sleeps == []


async def test_force_with_polish_accepts_a_long_rewrite_and_resets_the_gap(tmp_path):
    long_reply = "While you were away " + " ".join(["x"] * 25) + " 12 incidents opened."
    ctx, _, clock, drain = await setup(tmp_path, long_reply)
    narrator = ctx.narrator
    facts = "12 incidents opened."
    assert await narrator.say(facts, "high", force=True) == long_reply
    await narrator.say("after", "high", polish=False)
    await narrator.drain()
    assert texts(drain) == [long_reply, "after"]
    assert clock.sleeps == [4.0]


# polishing -----------------------------------------------------------------------------

GOOD = "r2-03 is healthy again after disk_full was fixed at the super rung in 8 steps for $0.011."


@pytest.mark.parametrize(
    "reply, expected",
    [
        (GOOD, GOOD),
        (f'"{GOOD}"\n', GOOD),
        (GOOD + " " + "so " * 10, FACTS),
        ("r2-03 is healthy again, disk_full fixed at the super rung in 9 steps for $0.011.", FACTS),
        ("r2-03 is healthy again, disk_full fixed at the super rung in 8 steps.", FACTS),
        ("r2-04 is healthy again, disk_full fixed at the super rung in 8 steps for $0.011.", FACTS),
        ("r2-03 is healthy again, the disk was fixed at the super rung in 8 steps for $0.011.", FACTS),
        ("r2-03 is healthy again.\nDisk fixed in 8 steps for $0.011.", FACTS),
        ("r2-03 is healthy again, disk_full fixed in 8 steps for $0.011!", FACTS),
        ("", FACTS),
    ],
)
async def test_polished_line_is_checked_against_the_facts(tmp_path, reply, expected):
    ctx, chat, _, drain = await setup(tmp_path, reply)
    await ctx.say(FACTS, "high")
    await ctx.narrator.drain()
    assert texts(drain) == [expected]
    call = chat.of("narrator")[0]
    assert call["thinking"] is False and call["model_key"] == "nano"
    assert call["messages"][1]["content"] == FACTS


@pytest.mark.parametrize("error", [RuntimeError("503"), BudgetExceeded(5.0, 5.0), asyncio.TimeoutError()])
async def test_model_failure_publishes_the_facts(tmp_path, error):
    ctx, _, _, drain = await setup(tmp_path, error)
    await ctx.say(FACTS, "high")
    await ctx.narrator.drain()
    assert texts(drain) == [FACTS]


async def test_accept_rules():
    assert accept("Storm: 4 nodes failed.", "Storm: 4 nodes failed within 10 seconds.") is False
    assert accept("Four nodes failed.", "4 nodes failed.") is False
    assert accept("4 nodes failed.", "4 nodes failed.") is True
    assert accept("a " * 19, "x") is False and accept("a " * 18, "x") is True
    assert accept("a " * 40, "x", max_words=None) is True
    assert accept("\u2705 fixed", "fixed") is False


# fact strings --------------------------------------------------------------------------


def worst_case_lines() -> list[str]:
    node = "r3-04"
    cls = "upstream_storm"
    return [
        context_module.budget_paused_line(123.45, 200.0),
        context_module.budget_resumed_line(),
        lines.storm_line(12, 10.0),
        lines.healthy_line(node, cls, "lightning", 14, 0.123),
        lines.unresolved_line(node, cls, "no progress for 90s: the model call did not finish"),
        lines.unresolved_line(node, cls, "a long unmatched reason with far more words than the limit allows here"),
        lines.resume_line(12),
        lines.promotion_line("log_permission", "lightning", "script", 5, 5, 0.0, 0.0075),
        lines.demotion_line("log_permission", "lightning", "super", 2, 5),
        lines.heldout_line("lightning", "log_permission 1 of 3, upstream_storm 1 of 1"),
        lines.rejection_line(node, "probe_b"),
        lines.rejection_line(node, "burst_3"),
        lines.escalation_line(node, "lightning", "super", "two fix claims rejected by the referee: probe A failed: HTTP 404"),
        lines.escalation_line(node, "script", "lightning", "referee rejected the script's fix: burst_2: connection reset"),
        lines.escalation_line(node, "lightning", "super", "read_config repeated 3 times with the same arguments"),
        lines.escalation_line(node, "lightning", "super", "step budget of 14 used without a verified fix"),
        lines.escalation_line(node, "lightning", "super", "model calls keep failing: APIConnectionError"),
        lines.escalation_line(node, "script", "lightning", "no compiled script for leaky_worker"),
        lines.escalation_line(node, "script", "lightning", "script failed after 12s: boom"),
        lines.escalation_line(node, "lightning", "super", "something else entirely: with detail that goes on"),
    ]


async def test_every_fact_string_fits_the_word_cap():
    for line in worst_case_lines():
        assert word_count(line) <= WORD_MAX, line
        assert accept(line, line)


async def test_rejection_line_names_the_failing_probe_in_plain_words():
    assert lines.rejection_line("r2-03", "burst_2") == "r2-03 reported fixed. The burst failed. Reopening."
    assert "The first health probe failed." in lines.rejection_line("r2-03", "probe_a")
    assert "The second health probe failed." in lines.rejection_line("r2-03", "probe_b")


async def test_short_reason_keeps_numbers_and_names():
    assert short_reason("no progress for 90s: the model call did not finish") == "no progress for 90s"
    assert short_reason("read_config repeated 3 times with the same arguments") == "read_config repeated 3 times"
    assert short_reason("") == "no reason recorded"
    assert short_reason("one two three four five six seven eight: nine") == "one two three four five six"


# wiring --------------------------------------------------------------------------------


class Said:
    def __init__(self):
        self.items: list[tuple[str, str]] = []

    async def say(self, text, priority="low", incident_id=None, polish=True, force=False):
        self.items.append((priority, text))
        return text


def incident_row(**extra):
    return {"id": 7, "node_id": "r2-03", "symptom": "probe failed", "zone": "live", **extra}


CLAIM = {"summary": "restarted", "root_cause": "stuck"}


async def test_first_rejection_is_narrated_and_the_second_is_left_to_the_escalation(tmp_path):
    ctx = await make_context(tmp_path, ScriptedChat(), FakeReferee([False, False, True]))
    said = ctx.narrator = Said()
    await ctx.verifier.review(incident_row(), CLAIM, "responder-1")
    assert said.items == [("high", "r2-03 reported fixed. The first health probe failed. Reopening.")]
    await ctx.verifier.review(incident_row(), CLAIM, "responder-1")
    assert len(said.items) == 1


async def test_forget_makes_the_next_responder_start_counting_again(tmp_path):
    ctx = await make_context(tmp_path, ScriptedChat(), FakeReferee([False, False]))
    said = ctx.narrator = Said()
    await ctx.verifier.review(incident_row(), CLAIM, "responder-1")
    ctx.verifier.forget(7)
    await ctx.verifier.review(incident_row(), CLAIM, "responder-2")
    assert len(said.items) == 2


async def test_no_rejection_line_for_scripts_trials_or_verified_fixes(tmp_path):
    chat = ScriptedChat(verifier=['{"quality": 5, "reason": "ok"}'])
    ctx = await make_context(tmp_path, chat, FakeReferee([False, False, True]))
    said = ctx.narrator = Said()
    await ctx.verifier.review(incident_row(), CLAIM, "script-1", grade=False)
    await ctx.verifier.review(incident_row(id=8, zone="proving"), CLAIM, "responder-1")
    await ctx.verifier.review(incident_row(id=9), CLAIM, "responder-1")
    assert said.items == []


async def test_escalation_is_narrated_with_a_short_reason(tmp_path):
    chat = ScriptedChat(
        triage=['{"fault_class": "leaky_worker", "confidence": 0.9}'],
        responder=["garbage"] * 4,
    )
    ctx = await make_context(tmp_path, chat)
    await ctx.db.set_ladder("leaky_worker", "lightning")
    said = ctx.narrator = Said()
    held = {}
    ctx.monitor = type("M", (), {"reset": lambda s, n: None, "hold": lambda s, n, t: held.update({n: t})})()
    iid = await ctx.db.open_incident(NODE, "probe failed", "low")
    await Commander(ctx).work(iid)
    assert said.items[0] == (
        "high", f"{NODE} escalated from lightning to super: no valid action from the model."
    )
    assert said.items[-1] == ("high", f"{NODE} (leaky_worker) is unresolved: no valid action from the model. It needs a person.")
    assert all(word_count(text) <= WORD_MAX for _, text in said.items)


async def test_two_rejections_in_a_live_run_give_one_rejection_line_and_one_escalation_line(tmp_path):
    chat = ScriptedChat(
        triage=['{"fault_class": "model_id_case", "confidence": 0.9}'],
        responder=[turn("claim_fixed", summary="restart", root_cause="a stuck process")] * 4,
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat, FakeReferee([False, False, True]))
    await ctx.db.set_ladder("model_id_case", "lightning")
    said = ctx.narrator = Said()
    ctx.monitor = type("M", (), {"reset": lambda s, n: None, "hold": lambda s, n, t: None})()
    iid = await ctx.db.open_incident(NODE, "probe failed", "low")
    await Commander(ctx).work(iid)
    assert [text for _, text in said.items[:2]] == [
        f"{NODE} reported fixed. The first health probe failed. Reopening.",
        f"{NODE} escalated from lightning to super: two fix claims rejected.",
    ]
    assert (await ctx.db.get_incident(iid))["status"] == "verified"
