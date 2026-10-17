import json
import os

import pytest

from tests.agent_support import NODE, OPEN, FakeReferee, ScriptedChat, collect, make_context, turn
from understudy.agents import protocol
from understudy.agents.commander import Commander
from understudy.agents.responder import Responder
from understudy.agents.tools import ToolBox
from understudy.llm import BudgetExceeded

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def close_databases():
    yield
    while OPEN:
        await OPEN.pop().close()


def incident(incident_id=1, node=NODE, symptom="probe failed: HTTP 404: model_not_found"):
    return {"id": incident_id, "node_id": node, "symptom": symptom, "severity": "low", "opened_at": 0.0,
            "fault_class": None, "rung": None, "escalations": 0, "status": "open"}


async def open_row(ctx, symptom="probe failed: HTTP 404: model_not_found"):
    iid = await ctx.db.open_incident(NODE, symptom, "low")
    return await ctx.db.get_incident(iid)


# tools ---------------------------------------------------------------------------------


async def toolbox(tmp_path):
    ctx = await make_context(tmp_path, ScriptedChat())
    iid = await ctx.db.open_incident(NODE, "probe failed", "low")
    return ctx, ToolBox(ctx, iid, NODE, "responder-1"), iid


async def test_file_tools_cannot_leave_the_node_directory(tmp_path):
    ctx, tools, iid = await toolbox(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    for rel in ("..", "../..", "/etc", str(outside), "data/../../secret.txt"):
        assert not (await tools.call(1, "list_dir", {"rel": rel})).ok
        assert not (await tools.call(1, "fix_permissions", {"rel": rel})).ok
    assert (await tools.call(1, "list_dir", {})).ok
    link = tools.paths.root / "link"
    link.symlink_to(tmp_path)
    assert not (await tools.call(1, "list_dir", {"rel": "link"})).ok
    assert outside.read_text() == "x"


async def test_delete_files_stays_in_data_and_keeps_newest_checkpoint(tmp_path):
    ctx, tools, _ = await toolbox(tmp_path)
    data = tools.paths.data
    for name in ("ckpt-1.bin", "ckpt-2.bin", "spool-1.bin", "spool-2.bin", "keep.txt"):
        (data / name).write_bytes(b"x" * 10)
    (tools.paths.root / "outside.bin").write_bytes(b"x")
    for glob in ("../*.bin", "/tmp/*", "data/*"):
        assert not (await tools.call(1, "delete_files", {"glob": glob})).ok
    done = await tools.call(1, "delete_files", {"glob": "*.bin"})
    assert done.ok and "kept newest checkpoint ckpt-2.bin" in done.text
    assert sorted(p.name for p in data.iterdir()) == ["ckpt-2.bin", "keep.txt"]
    assert (tools.paths.root / "outside.bin").exists()


async def test_write_config_validates_and_keeps_a_backup(tmp_path):
    ctx, tools, _ = await toolbox(tmp_path)
    original = tools.paths.config.read_text()
    bad = await tools.call(1, "write_config", {"yaml_text": "model: [unclosed"})
    assert not bad.ok and "not written" in bad.text
    assert tools.paths.config.read_text() == original
    no_model = await tools.call(1, "write_config", {"yaml_text": "port: 19101\n"})
    assert not no_model.ok
    foreign_port = await tools.call(1, "write_config", {"yaml_text": original.replace("19101", "8000")})
    assert not foreign_port.ok and tools.paths.config.read_text() == original
    renamed = await tools.call(1, "write_config", {"yaml_text": original.replace(f"node_id: {NODE}", "node_id: r2-02")})
    assert not renamed.ok
    edited = original.replace("max_tokens: 256", "max_tokens: 300")
    ok = await tools.call(1, "write_config", {"yaml_text": edited})
    assert ok.ok
    assert "max_tokens: 300" in tools.paths.config.read_text()
    backups = list(tools.paths.root.glob("config.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == original


async def test_set_port_and_squatter_rules(tmp_path):
    ctx, tools, _ = await toolbox(tmp_path)
    assert not (await tools.call(1, "set_port", {"port": 9101})).ok
    assert not (await tools.call(1, "set_port", {"port": "abc"})).ok
    assert (await tools.call(1, "set_port", {"port": 9250})).ok
    assert ctx.sup.port_of(NODE) == 9250
    assert not (await tools.call(1, "kill_squatter", {"pid": os.getpid()})).ok
    assert not (await tools.call(1, "kill_squatter", {"pid": "x"})).ok


async def test_claim_needs_a_root_cause_and_calls_are_recorded(tmp_path):
    ctx, tools, iid = await toolbox(tmp_path)
    drain = collect(ctx.bus)
    weak = await tools.call(1, "claim_fixed", {"summary": "restarted"})
    assert not weak.ok and weak.claim is None
    claim = await tools.call(2, "claim_fixed", {"summary": "set model id", "root_cause": "model id had wrong casing"})
    assert claim.ok and claim.claim == {"summary": "set model id", "root_cause": "model id had wrong casing"}
    unknown = await tools.call(3, "rm_rf", {})
    assert not unknown.ok and "unknown tool" in unknown.text
    actions = await ctx.db.actions_for(iid)
    assert [a["tool"] for a in actions] == ["claim_fixed", "claim_fixed", "rm_rf"]
    assert [e.data["ok"] for e in drain() if e.type == "tool_call"] == [False, True, False]


async def test_read_tools_work_on_a_node_that_is_not_running(tmp_path):
    ctx, tools, _ = await toolbox(tmp_path)
    status = await tools.call(1, "node_status", {})
    assert status.ok and "not started" in status.text
    assert "model:" in (await tools.call(2, "read_config", {})).text
    assert (await tools.call(3, "tail_log", {"n": 500})).ok
    assert "config.yaml" in (await tools.call(4, "list_dir", {"rel": "."})).text


async def test_read_config_reads_other_files_inside_the_node_only(tmp_path):
    ctx, tools, _ = await toolbox(tmp_path)
    (tools.paths.root / "exit.json").write_text('{"code": 3}')
    assert (await tools.call(1, "read_config", {"rel": "exit.json"})).text == '{"code": 3}'
    assert not (await tools.call(2, "read_config", {"rel": "../../pyproject.toml"})).ok
    assert not (await tools.call(3, "read_config", {"rel": "data"})).ok
    assert not (await tools.call(4, "read_config", {"rel": "missing.txt"})).ok


# responder -----------------------------------------------------------------------------


async def run_responder(ctx, rung="super", handoff=None):
    row = await open_row(ctx)
    return row, await Responder(ctx, row, rung, "model_id_case", ("model_id_case", 0.9), handoff).run()


async def test_rejected_claim_goes_back_with_the_evidence_and_the_second_claim_verifies(tmp_path):
    chat = ScriptedChat(
        responder=[
            turn("read_config"),
            turn("claim_fixed", summary="restarted", root_cause="the node was stuck"),
            turn("tail_log", n=20),
            turn("claim_fixed", summary="set the model id", root_cause="model id had the wrong casing"),
        ],
        verifier=['{"quality": 5, "reason": "minimal and correct"}'],
    )
    ctx = await make_context(tmp_path, chat, FakeReferee([False, True]))
    drain = collect(ctx.bus)
    row, outcome = await run_responder(ctx)
    assert outcome.status == "verified" and outcome.quality == 5 and outcome.steps == 4
    third = chat.of("responder")[2]["messages"]
    assert third[-1]["content"].startswith("Health check failed: ")
    assert "HTTP 404: model_not_found" in third[-1]["content"]
    assert third[-1]["content"].endswith("The node is not healthy. Continue.")
    types = [e.type for e in drain()]
    assert types.count("fix_claimed") == 2 and types.count("verification") == 2
    assert types[0] == "agent_spawned" and types[-1] == "agent_done"
    verdicts = [e.data["verdict"] for e in drain() if e.type == "verification"]
    assert verdicts == ["rejected", "verified"]
    rows = await ctx.db.fetchall("SELECT verdict, quality FROM verifications ORDER BY id")
    assert [(r["verdict"], r["quality"]) for r in rows] == [("rejected", None), ("verified", 5)]


async def test_repeated_call_is_told_the_answer_has_not_changed(tmp_path):
    chat = ScriptedChat(
        responder=[
            turn("node_status"),
            turn("node_status"),
            turn("claim_fixed", summary="done", root_cause="the model id was wrong"),
        ],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat)
    await run_responder(ctx)
    messages = chat.of("responder")[2]["messages"]
    assert "exact call" in messages[-1]["content"]
    assert "exact call" not in chat.of("responder")[1]["messages"][-1]["content"]


async def test_two_rejections_escalate(tmp_path):
    chat = ScriptedChat(
        responder=[turn("claim_fixed", summary="restart", root_cause="a stuck process in the node")] * 2
    )
    ctx = await make_context(tmp_path, chat, FakeReferee([False, False]))
    _, outcome = await run_responder(ctx)
    assert outcome.status == "escalate" and "two fix claims rejected" in outcome.reason


async def test_unparseable_reply_gets_one_reprompt(tmp_path):
    chat = ScriptedChat(
        responder=[
            "I think we should look at the logs.",
            turn("claim_fixed", summary="done", root_cause="the model id was wrong"),
        ],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat)
    _, outcome = await run_responder(ctx)
    assert outcome.status == "verified"
    retry = chat.of("responder")[1]["messages"]
    assert retry[-1]["content"] == protocol.REPROMPT and retry[-2]["role"] == "assistant"


async def test_second_unparseable_reply_escalates(tmp_path):
    chat = ScriptedChat(responder=["nope", "still nope"])
    ctx = await make_context(tmp_path, chat)
    _, outcome = await run_responder(ctx)
    assert outcome.status == "escalate" and "valid action" in outcome.reason


async def test_repeating_an_action_three_times_escalates(tmp_path):
    chat = ScriptedChat(responder=[turn("node_status")] * 5)
    ctx = await make_context(tmp_path, chat)
    _, outcome = await run_responder(ctx)
    assert outcome.status == "escalate" and "repeated 3 times" in outcome.reason
    assert len(chat.of("responder")) == 3


async def test_step_budget_escalates(tmp_path):
    chat = ScriptedChat(responder=[turn("tail_log", n=n) for n in range(10, 100, 10)])
    ctx = await make_context(tmp_path, chat, max_steps=4)
    _, outcome = await run_responder(ctx)
    assert outcome.status == "escalate" and "step budget" in outcome.reason and outcome.steps == 4


async def test_model_errors_escalate_and_budget_errors_propagate(tmp_path):
    chat = ScriptedChat(responder=[RuntimeError("boom"), RuntimeError("boom again")])
    ctx = await make_context(tmp_path, chat)
    _, outcome = await run_responder(ctx)
    assert outcome.status == "escalate" and "keep failing" in outcome.reason
    chat = ScriptedChat(responder=[BudgetExceeded(5.0, 5.0)])
    ctx = await make_context(tmp_path / "b", chat)
    with pytest.raises(BudgetExceeded):
        await run_responder(ctx)


async def test_script_rung_without_a_script_escalates_and_with_one_claims(tmp_path):
    ctx = await make_context(tmp_path, ScriptedChat(), scripts_dir=tmp_path / "scripts")
    _, outcome = await run_responder(ctx, "script")
    assert outcome.status == "escalate" and "no compiled script" in outcome.reason
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "model_id_case.py").write_text("import sys\nprint('fixed', sys.argv[1])\n")
    ctx = await make_context(
        tmp_path / "two", ScriptedChat(verifier=['{"quality": 4, "reason": "ok"}']), scripts_dir=tmp_path / "scripts"
    )
    row, outcome = await run_responder(ctx, "script")
    assert outcome.status == "verified"
    assert (await ctx.db.actions_for(row["id"]))[0]["tool"] == "run_script"


# commander -----------------------------------------------------------------------------


def triage_reply(name, confidence):
    return json.dumps({"fault_class": name, "confidence": confidence})


async def test_commander_closes_a_verified_incident(tmp_path):
    chat = ScriptedChat(
        triage=[triage_reply("model_id_case", 0.9)],
        responder=[turn("claim_fixed", summary="fixed model id", root_cause="model id had the wrong casing")],
        verifier=['{"quality": 5, "reason": "minimal"}'],
    )
    ctx = await make_context(tmp_path, chat)
    ctx.monitor = type("M", (), {"reset": lambda self, n: setattr(self, "last", n)})()
    await ctx.db.add_fault_truth(NODE, "model_id_case", 5)
    drain = collect(ctx.bus)
    row = await open_row(ctx)
    await ctx.db.record_llm_call(role="responder", model_key="super", model="m", cost_usd=0.002, incident_id=row["id"])
    await ctx.db.record_llm_call(role="responder", model_key="super", model="m", cost_usd=0.5, incident_id=row["id"] + 1)
    commander = Commander(ctx)
    await commander.work(row["id"])
    closed = await ctx.db.get_incident(row["id"])
    assert closed["status"] == "verified" and closed["fault_class"] == "model_id_case"
    assert closed["rung"] == "super" and closed["quality"] == 5 and closed["steps"] == 1
    assert closed["cost_usd"] == pytest.approx(0.002) and closed["closed_at"] is not None
    assert await ctx.db.active_faults(NODE) == []
    assert ctx.state.status(NODE) == "healthy" and ctx.monitor.last == NODE
    assert [e.type for e in drain() if e.type == "incident_closed"] == ["incident_closed"]
    assert chat.of("triage")[0]["model_key"] == "nano" and chat.of("triage")[0]["thinking"] is False


async def test_low_confidence_triage_goes_to_super(tmp_path):
    chat = ScriptedChat(
        triage=[triage_reply("disk_full", 0.3), triage_reply("model_id_case", 0.8)],
        responder=[turn("claim_fixed", summary="fixed", root_cause="wrong model id casing in config")],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat)
    row = await open_row(ctx)
    await Commander(ctx).work(row["id"])
    assert [c["model_key"] for c in chat.of("triage")][:2] == ["nano", "super"]
    assert (await ctx.db.get_incident(row["id"]))["fault_class"] == "model_id_case"


async def test_failing_responder_escalates_up_the_ladder_then_is_unresolved(tmp_path):
    chat = ScriptedChat(
        triage=[triage_reply("leaky_worker", 0.9)],
        responder=["garbage", "garbage", "garbage", "garbage"],
    )
    ctx = await make_context(tmp_path, chat)
    await ctx.db.set_ladder("leaky_worker", "script")
    held = {}
    ctx.monitor = type("M", (), {"reset": lambda s, n: None, "hold": lambda s, n, t: held.update({n: t})})()
    ctx.scripts_dir = tmp_path / "none"
    drain = collect(ctx.bus)
    row = await open_row(ctx)
    await Commander(ctx).work(row["id"])
    closed = await ctx.db.get_incident(row["id"])
    assert closed["status"] == "unresolved" and closed["escalations"] == 2
    steps = [(e.data["from_rung"], e.data["to_rung"]) for e in drain() if e.type == "escalation"]
    assert steps == [("script", "lightning"), ("lightning", "super")]
    assert [c["model_key"] for c in chat.of("responder")] == ["lightning", "lightning", "super", "super"]
    assert ctx.state.status(NODE) == "down" and NODE in held


async def test_budget_exhaustion_pauses_and_the_incident_resumes(tmp_path):
    chat = ScriptedChat(
        triage=[BudgetExceeded(5, 5), triage_reply("model_id_case", 0.9)],
        responder=[turn("claim_fixed", summary="fixed", root_cause="model id wrong casing")],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat)
    waits = []

    async def wait():
        waits.append(1)

    ctx.budget_wait = wait  # type: ignore[method-assign]
    row = await open_row(ctx)
    await Commander(ctx).work(row["id"])
    assert waits == [1] and (await ctx.db.get_incident(row["id"]))["status"] == "verified"


async def test_resumed_incident_keeps_its_rung_and_sees_earlier_actions(tmp_path):
    chat = ScriptedChat(
        responder=[turn("claim_fixed", summary="fixed", root_cause="model id wrong casing")],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat)
    row = await open_row(ctx)
    await ctx.db.update_incident(row["id"], fault_class="model_id_case", rung="lightning", escalations=1)
    await ctx.db.add_action(row["id"], "responder-9", 1, "read_config", {}, True, "model: nvidia/x")
    await Commander(ctx).work(row["id"], resumed=True)
    call = chat.of("responder")[0]
    assert call["model_key"] == "lightning"
    context = call["messages"][2]["content"]
    assert "Earlier attempt" in context and "read_config" in context and "restarted" in context
    assert all("Root cause stated" in c["messages"][-1]["content"] for c in chat.of("triage"))


async def test_resume_announces_open_incidents(tmp_path):
    chat = ScriptedChat(
        triage=[triage_reply("model_id_case", 0.9)],
        responder=[turn("claim_fixed", summary="fixed", root_cause="model id wrong casing")],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat)
    said = []

    async def say(text, priority="low", incident_id=None, polish=True):
        said.append(text)
        return text

    ctx.narrator = type("N", (), {"say": staticmethod(say)})()
    await open_row(ctx)
    commander = Commander(ctx)
    assert await commander.resume() == 1
    assert said == ["Resuming 1 open incidents."]
    assert ctx.state.status(NODE) == "repairing"
    await commander.stop()


async def interrupted_claim(ctx, row):
    await ctx.db.update_incident(row["id"], fault_class="model_id_case", rung="super")
    await ctx.db.add_action(row["id"], "responder-9", 1, "read_config", {}, True, "model: nvidia/x")
    await ctx.db.add_action(
        row["id"], "responder-9", 2, "claim_fixed",
        {"summary": "fixed the model id", "root_cause": "model id had the wrong casing"}, True, "claim recorded",
    )


async def test_claim_pending_at_restart_is_ruled_on_without_a_new_responder(tmp_path):
    chat = ScriptedChat(verifier=['{"quality": 5, "reason": "minimal"}'])
    referee = FakeReferee([True])
    ctx = await make_context(tmp_path, chat, referee)
    row = await open_row(ctx)
    await interrupted_claim(ctx, row)
    await Commander(ctx).work(row["id"], resumed=True)
    closed = await ctx.db.get_incident(row["id"])
    assert closed["status"] == "verified" and closed["quality"] == 5 and closed["fault_class"] == "model_id_case"
    assert referee.verified == [NODE] and not chat.of("responder")


async def test_rejected_pending_claim_is_handed_to_the_new_responder(tmp_path):
    chat = ScriptedChat(
        responder=[turn("claim_fixed", summary="set the id", root_cause="model id had the wrong casing")],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    ctx = await make_context(tmp_path, chat, FakeReferee([False, True]))
    row = await open_row(ctx)
    await interrupted_claim(ctx, row)
    await Commander(ctx).work(row["id"], resumed=True)
    handoff = chat.of("responder")[0]["messages"][2]["content"]
    assert "referee rejected the claim that was pending" in handoff and "HTTP 404" in handoff
    assert (await ctx.db.get_incident(row["id"]))["status"] == "verified"


async def test_claim_already_ruled_on_is_not_rechecked(tmp_path):
    chat = ScriptedChat(
        responder=[turn("claim_fixed", summary="set the id", root_cause="model id had the wrong casing")],
        verifier=['{"quality": 4, "reason": "ok"}'],
    )
    referee = FakeReferee([True])
    ctx = await make_context(tmp_path, chat, referee)
    row = await open_row(ctx)
    await interrupted_claim(ctx, row)
    await ctx.db.add_verification(row["id"], NODE, "rejected", [], None, "probe A failed")
    await Commander(ctx).work(row["id"], resumed=True)
    assert len(chat.of("responder")) == 1 and referee.verified == [NODE]
