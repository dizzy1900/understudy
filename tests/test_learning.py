"""Librarian, promoter and demotion with a scripted model, a scripted referee and a faked fault layer."""
import json
import time

import pytest

from understudy import ledger
from tests.agent_support import NODE, OPEN, FakeReferee, ScriptedChat, collect, make_context, turn
from understudy.agents import librarian as lib
from understudy.agents import promoter as prom
from understudy.agents.librarian import Librarian, fit_card
from understudy.agents.promoter import Promoter

pytestmark = pytest.mark.anyio

GOOD_SCRIPT = (
    "import yaml\n\ndef fix(tools):\n    cfg = yaml.safe_load(tools.read_config().text)\n"
    "    cfg['model'] = 'x'\n    tools.write_config(yaml_text=yaml.safe_dump(cfg))\n"
    "    return 'set the model', 'the model id was wrong'\n"
)


@pytest.fixture(autouse=True)
async def close_databases():
    yield
    while OPEN:
        await OPEN.pop().close()


def card_reply(signature="HTTP 404 model_not_found", fix=("read_config", "write_config the canonical model id", "restart_node"), change="first card"):
    return json.dumps(
        {"signature": signature, "diagnosis": "The model id in config.yaml differs in case from the canonical id.",
         "fix": list(fix), "pitfalls": "A restart alone does not fix it.", "change": change}
    )


async def seed_fix(ctx, fault_class="model_id_case", quality=5, rung="super", steps=6, cost=0.01, escalations=0, closed=None):
    iid = await ctx.db.open_incident(NODE, "probe failed: HTTP 404: model_not_found", "low")
    await ctx.db.update_incident(
        iid, status="verified", fault_class=fault_class, rung=rung, steps=steps, cost_usd=cost, quality=quality,
        escalations=escalations, closed_at=time.time() if closed is None else closed,
    )
    await ctx.db.add_action(iid, "r-1", 1, "read_config", {}, True, "model: nvidia/NVIDIA-nemotron")
    await ctx.db.add_action(
        iid, "r-1", 2, "claim_fixed", {"summary": "set the model id", "root_cause": "model id had the wrong casing"}, True, "ok"
    )
    return iid


# librarian -----------------------------------------------------------------------------


async def test_card_is_written_within_the_limit_with_evidence_from_the_database(tmp_path):
    chat = ScriptedChat(librarian=[card_reply()])
    ctx = await make_context(tmp_path, chat)
    for steps in (4, 6, 8):
        await seed_fix(ctx, steps=steps)
    drain = collect(ctx.bus)
    version = await Librarian(ctx).update_card("model_id_case")
    assert version == 1
    (row,) = await ctx.db.latest_skills()
    text = lib.protocol.render_card(row)
    assert len(text) <= lib.CARD_LIMIT and text.startswith("## model_id_case\nSignature: HTTP 404")
    assert text.endswith("Evidence: 3 verified fixes, mean 6.0 steps")
    assert "Fix: 1. read_config 2. write_config the canonical model id 3. restart_node" in text
    assert [e.data for e in drain() if e.type == "skill_updated"] == [{"fault_class": "model_id_case", "version": 1, "summary": "first card"}]
    prompt = chat.of("librarian")[0]
    assert prompt["thinking"] is True and prompt["model_key"] == "super"
    assert prompt["messages"][1]["content"].count("Verified fix") == 3


async def test_unchanged_card_does_not_make_a_new_version_and_a_change_does(tmp_path):
    chat = ScriptedChat(librarian=[card_reply(), card_reply(signature="  http 404  MODEL_NOT_FOUND "), card_reply(signature="HTTP 404 model_not_found on every probe", change="more exact")])
    ctx = await make_context(tmp_path, chat)
    await seed_fix(ctx)
    librarian = Librarian(ctx)
    assert await librarian.update_card("model_id_case") == 1
    assert await librarian.update_card("model_id_case") is None
    assert await librarian.update_card("model_id_case") == 2
    assert len(await ctx.db.fetchall("SELECT * FROM skills")) == 2
    block_before = lib.protocol.skill_block(await ctx.db.latest_skills())
    assert "on every probe" in block_before


async def test_oversized_card_is_trimmed_to_nine_hundred_characters(tmp_path):
    long = "x" * 600
    card = fit_card("model_id_case", {"signature": long, "diagnosis": long, "fix_steps": [long] * 5, "pitfalls": long, "evidence": "3 verified fixes, mean 6.0 steps"})
    assert lib.render_length("model_id_case", card) <= lib.CARD_LIMIT
    assert card["evidence"].startswith("3 verified")


async def test_only_good_fixes_trigger_updates_and_updates_are_spaced(tmp_path):
    chat = ScriptedChat(librarian=[card_reply(), card_reply(signature="a second wording of the signature")])
    ctx = await make_context(tmp_path, chat)
    await seed_fix(ctx)
    librarian = Librarian(ctx)
    await librarian.after_verified("model_id_case", 3)
    await librarian.after_verified("model_id_case", None)
    await librarian.after_verified("not_a_class", 5)
    assert not chat.of("librarian")
    await librarian.after_verified("model_id_case", 5)
    assert len(chat.of("librarian")) == 1
    await librarian.after_verified("model_id_case", 5)
    assert len(chat.of("librarian")) == 1 and "model_id_case" in librarian._waiting
    await librarian.stop()


async def test_backfill_writes_a_card_only_for_classes_with_fixes_and_no_card(tmp_path):
    chat = ScriptedChat(librarian=[card_reply()])
    ctx = await make_context(tmp_path, chat)
    await seed_fix(ctx)
    await seed_fix(ctx, fault_class="disk_full", quality=2)
    await Librarian(ctx).backfill()
    assert [r["fault_class"] for r in await ctx.db.latest_skills()] == ["model_id_case"]


async def test_heldout_results_and_failures_reach_the_card_prompt(tmp_path):
    chat = ScriptedChat(librarian=[card_reply()])
    ctx = await make_context(tmp_path, chat)
    await seed_fix(ctx)
    await ctx.db.add_heldout_report("proving", "model_id_case", "lightning", False, seed=100001, detail={"reason": "restarted without fixing the config"})
    await ctx.db.add_heldout_report("proving", "model_id_case", "lightning", True, seed=100002)
    await Librarian(ctx).update_card("model_id_case")
    prompt = chat.of("librarian")[0]["messages"][1]["content"]
    assert "lightning rung: 1 of 2 held-out instances fixed" in prompt and "restarted without fixing the config" in prompt


async def test_script_is_compiled_only_for_a_lightning_class_with_five_fixes(tmp_path):
    reply = f"```python\n{GOOD_SCRIPT}```"
    chat = ScriptedChat(librarian=[reply])
    ctx = await make_context(tmp_path, chat, scripts_dir=tmp_path / "scripts")
    librarian = Librarian(ctx)
    for _ in range(4):
        await seed_fix(ctx, rung="lightning")
    await ctx.db.set_ladder("model_id_case", "lightning")
    assert not await librarian.maybe_compile("model_id_case")
    await seed_fix(ctx, rung="lightning")
    await ctx.db.set_ladder("model_id_case", "super")
    assert not await librarian.maybe_compile("model_id_case")
    await ctx.db.set_ladder("model_id_case", "lightning")
    assert await librarian.maybe_compile("model_id_case")
    path = tmp_path / "scripts" / "model_id_case.py"
    assert "def fix(tools)" in path.read_text()
    assert not await librarian.maybe_compile("model_id_case"), "an existing script is not rewritten"


async def test_rejected_script_is_sent_back_once_then_accepted(tmp_path):
    chat = ScriptedChat(librarian=["```python\nimport os\ndef fix(tools):\n    return 'a', 'b'\n```", f"```python\n{GOOD_SCRIPT}```"])
    ctx = await make_context(tmp_path, chat, scripts_dir=tmp_path / "scripts")
    assert await Librarian(ctx).compile_script("model_id_case")
    second = chat.of("librarian")[1]["messages"][-1]["content"]
    assert "rejected" in second and "'os'" in second
    chat = ScriptedChat(librarian=["not code at all"] * 2)
    ctx = await make_context(tmp_path / "b", chat, scripts_dir=tmp_path / "scripts2")
    assert not await Librarian(ctx).compile_script("model_id_case")
    assert not (tmp_path / "scripts2" / "model_id_case.py").exists()


# promoter ------------------------------------------------------------------------------


async def promoter_context(tmp_path, chat, referee=None, **kwargs):
    ctx = await make_context(tmp_path, chat, referee or FakeReferee(), proving=4, **kwargs)
    planted = []

    async def inject(sup, node_id, fault_class, seed, db=None):
        planted.append((node_id, fault_class, seed))
        await db.add_fault_truth(node_id, fault_class, seed, {}, zone="proving")

    async def reset(node_id, start=True):
        return None

    said = []

    async def say(text, priority="low", incident_id=None, polish=True):
        said.append((priority, text))
        return text

    ctx.sup.reset = reset  # type: ignore[method-assign]
    ctx.narrator = type("N", (), {"say": staticmethod(say)})()
    ctx.planted = planted
    ctx.said = said
    return ctx, inject


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(prom, "SYMPTOM_GAP_S", 0.0)
    monkeypatch.setattr(prom, "SYMPTOM_PROBES", 1)


async def make_promoter(ctx, inject, monkeypatch):
    monkeypatch.setattr(prom.faults, "inject", inject)
    promoter = Promoter(ctx)
    for node in promoter.nodes:
        promoter._pool.put_nowait(node)
    return promoter


def trial_chat(claims=40):
    triage = json.dumps({"fault_class": "model_id_case", "confidence": 0.9})
    return ScriptedChat(
        triage=[triage] * claims,
        responder=[turn("claim_fixed", summary="set the id", root_cause="model id had the wrong casing")] * claims,
        verifier=['{"quality": 5, "reason": "minimal"}'] * claims,
    )


async def test_plan_orders_classes_and_explains_what_it_leaves_out(tmp_path, monkeypatch, fast):
    ctx, inject = await promoter_context(tmp_path, trial_chat())
    promoter = await make_promoter(ctx, inject, monkeypatch)
    for _ in range(3):
        await seed_fix(ctx)
    await seed_fix(ctx, fault_class="disk_full")
    await ctx.db.save_skill("model_id_case", {"signature": "404"})
    await ctx.db.set_ladder("bad_key_env", "script")
    plan, skipped = await promoter.plan(4)
    assert plan == [("model_id_case", "super", "lightning")]
    assert "needs 2" in skipped["disk_full"] and "cheapest" in skipped["bad_key_env"]
    await ctx.db.set_ladder("config_syntax", "lightning")
    _plan, skipped = await promoter.plan(4)
    assert skipped["config_syntax"] == "no compiled script yet"
    forced, _ = await promoter.plan(4, only=["disk_full"])
    assert forced == [("disk_full", "super", "lightning")]


async def test_class_is_promoted_after_four_of_five_held_out_instances_and_lower_cost(tmp_path, monkeypatch, fast):
    ctx, inject = await promoter_context(tmp_path, trial_chat())
    promoter = await make_promoter(ctx, inject, monkeypatch)
    for _ in range(3):
        await seed_fix(ctx, cost=0.02)
    drain = collect(ctx.bus)
    result = await promoter.run_now()
    assert result["started"] and result["classes"][0] == {"fault_class": "model_id_case", "from": "super", "to": "lightning"}
    await promoter._cycle
    assert (await ctx.db.get_ladder())["model_id_case"] == "lightning"
    (event,) = await ctx.db.fetchall("SELECT * FROM ladder_events")
    assert (event["direction"], event["from_rung"], event["to_rung"], event["passed"], event["total"]) == ("promote", "super", "lightning", 5, 5)
    reports = await ctx.db.fetchall("SELECT * FROM heldout_reports")
    assert len(reports) == 5 and all(r["kind"] == "proving" and r["rung"] == "lightning" and r["passed"] == 1 for r in reports)
    assert all(r["seed"] >= 100_000 for r in reports) and len({r["seed"] for r in reports}) == 5
    assert {n for n, _c, _s in ctx.planted} <= set(promoter.nodes)
    events = drain()
    assert [e.data["direction"] for e in events if e.type == "ladder_update"] == ["promote"]
    high = [text for priority, text in ctx.said if priority == "high"]
    assert high and "5 of 5" in high[0] and "$0.0200" in high[0]
    proving = await ctx.db.fetchall("SELECT * FROM incidents WHERE zone = 'proving'")
    assert len(proving) == 5 and all(p["status"] == "verified" and p["rung"] == "lightning" for p in proving)
    assert all(ctx.state.status(n) == "healthy" for n in promoter.nodes)
    assert await ctx.db.active_faults() == []
    await ctx.db.set_setting("ledger_since", "0")
    led = await ledger.compute(ctx.db)
    assert led.incidents_total == 3 and led.verified == 3 and led.promotions == 1


async def test_no_promotion_when_the_trials_do_not_clear_the_bar(tmp_path, monkeypatch, fast):
    class Picky(FakeReferee):
        async def verify(self, node_id):
            from tests.agent_support import verdict
            return verdict(node_id, node_id in ("pg-03", "pg-04"))

    ctx, inject = await promoter_context(tmp_path, trial_chat(), Picky())
    promoter = await make_promoter(ctx, inject, monkeypatch)
    for _ in range(3):
        await seed_fix(ctx)
    await promoter.run_now()
    await promoter._cycle
    assert (await ctx.db.get_ladder())["model_id_case"] == "super"
    assert await ctx.db.fetchall("SELECT * FROM ladder_events") == []
    reports = await ctx.db.fetchall("SELECT * FROM heldout_reports")
    assert 4 <= len(reports) <= 5 and sum(r["passed"] for r in reports) <= 3
    failed = [json.loads(r["detail_json"]) for r in reports if not r["passed"]]
    assert failed and all(d["escalated"] and "rejected" in d["reason"] for d in failed)


async def test_promotion_needs_a_lower_mean_cost(tmp_path, monkeypatch, fast):
    ctx, inject = await promoter_context(tmp_path, trial_chat())
    promoter = await make_promoter(ctx, inject, monkeypatch)
    for _ in range(3):
        await seed_fix(ctx, cost=0.0)
    await promoter.run_now()
    await promoter._cycle
    assert (await ctx.db.get_ladder())["model_id_case"] == "super"


def api_failure_chat(failures):
    chat = trial_chat(4)
    chat.queues["responder"] = [RuntimeError("429 too many requests")] * failures + chat.queues["responder"]
    return chat


async def test_trial_is_run_again_when_the_model_api_fails_and_only_the_good_run_counts(tmp_path, monkeypatch, fast):
    ctx, inject = await promoter_context(tmp_path, api_failure_chat(2))
    promoter = await make_promoter(ctx, inject, monkeypatch)
    trial = await promoter._trial("pg-01", "model_id_case", "lightning", 100_001, "proving")
    assert trial.passed and not trial.void
    reports = await ctx.db.fetchall("SELECT * FROM heldout_reports")
    assert len(reports) == 1 and reports[0]["passed"] == 1
    statuses = [r["status"] for r in await ctx.db.fetchall("SELECT status FROM incidents WHERE zone = 'proving' ORDER BY id")]
    assert statuses == ["unresolved", "verified"]


async def test_trial_is_voided_not_failed_when_the_model_api_never_recovers(tmp_path, monkeypatch, fast):
    ctx, inject = await promoter_context(tmp_path, api_failure_chat(8))
    promoter = await make_promoter(ctx, inject, monkeypatch)
    trial = await promoter._trial("pg-01", "model_id_case", "lightning", 100_001, "proving")
    assert trial.void and not trial.passed
    assert await ctx.db.fetchall("SELECT * FROM heldout_reports") == []
    assert all(r["status"] == "unresolved" for r in await ctx.db.fetchall("SELECT status FROM incidents WHERE zone = 'proving'"))


async def test_run_now_reports_when_nothing_is_ready_or_a_run_is_active(tmp_path, monkeypatch, fast):
    ctx, inject = await promoter_context(tmp_path, trial_chat())
    promoter = await make_promoter(ctx, inject, monkeypatch)
    none = await promoter.run_now()
    assert not none["started"] and none["skipped"]["model_id_case"].startswith("0 verified")
    for _ in range(3):
        await seed_fix(ctx)
    assert (await promoter.run_now())["started"]
    busy = await promoter.run_now()
    assert not busy["started"] and "in progress" in busy["reason"]
    await promoter._cycle


async def test_heldout_classes_are_run_at_lightning_and_failures_are_kept(tmp_path, monkeypatch, fast):
    ctx, inject = await promoter_context(tmp_path, trial_chat(), FakeReferee([True] * 3 + [False] * 6))
    promoter = await make_promoter(ctx, inject, monkeypatch)
    trials = await promoter.run_heldout()
    assert len(trials) == 6
    reports = await ctx.db.fetchall("SELECT * FROM heldout_reports")
    assert {r["kind"] for r in reports} == {"heldout_class"} and {r["rung"] for r in reports} == {"lightning"}
    assert {r["fault_class"] for r in reports} == {"log_permission", "upstream_storm"}
    assert any(not r["passed"] for r in reports)
    assert set((await ctx.db.get_ladder()).values()) <= {"super"}


# demotion ------------------------------------------------------------------------------


async def demotion_setup(tmp_path, monkeypatch, outcomes):
    ctx, inject = await promoter_context(tmp_path, trial_chat())
    promoter = await make_promoter(ctx, inject, monkeypatch)
    await ctx.db.set_ladder("model_id_case", "lightning")
    await ctx.db.add_ladder_event("model_id_case", "super", "lightning", "promote", 5, 5, 0.001)
    await ctx.db.execute("UPDATE ladder_events SET ts = ?", (time.time() - 600,))
    for i, clean in enumerate(outcomes):
        await seed_fix(ctx, rung="lightning" if clean else "super", escalations=0 if clean else 1, closed=time.time() - 300 + i)
    return ctx, promoter


async def test_class_is_demoted_when_fewer_than_sixty_percent_are_clean(tmp_path, monkeypatch):
    ctx, promoter = await demotion_setup(tmp_path, monkeypatch, [True, False, False, True, False])
    drain = collect(ctx.bus)
    await promoter.after_incident("model_id_case")
    assert (await ctx.db.get_ladder())["model_id_case"] == "super"
    events = await ctx.db.fetchall("SELECT * FROM ladder_events ORDER BY id")
    assert (events[-1]["direction"], events[-1]["from_rung"], events[-1]["to_rung"], events[-1]["passed"], events[-1]["total"]) == ("demote", "lightning", "super", 2, 5)
    assert [e.data["direction"] for e in drain() if e.type == "ladder_update"] == ["demote"]


async def test_class_stays_when_three_of_five_are_clean_or_there_are_too_few_outcomes(tmp_path, monkeypatch):
    ctx, promoter = await demotion_setup(tmp_path, monkeypatch, [True, True, False, True, False])
    await promoter.after_incident("model_id_case")
    assert (await ctx.db.get_ladder())["model_id_case"] == "lightning"
    ctx, promoter = await demotion_setup(tmp_path / "few", monkeypatch, [False, False, False, False])
    await promoter.after_incident("model_id_case")
    assert (await ctx.db.get_ladder())["model_id_case"] == "lightning"


async def test_outcomes_from_before_the_promotion_do_not_count(tmp_path, monkeypatch):
    ctx, promoter = await demotion_setup(tmp_path, monkeypatch, [False] * 5)
    await ctx.db.execute("UPDATE ladder_events SET ts = ?", (time.time(),))
    await promoter.after_incident("model_id_case")
    assert (await ctx.db.get_ladder())["model_id_case"] == "lightning"
