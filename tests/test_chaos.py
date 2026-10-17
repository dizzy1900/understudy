import time
from types import SimpleNamespace

import pytest

from config import settings
from tests.agent_support import FakeReferee, ScriptedChat, collect
from understudy import llm
from understudy.agents import chaos as chaos_module
from understudy.agents.chaos import (
    CONFIRMATION_WORDS,
    MAX_DOWN,
    ChaosError,
    ChaosOps,
    ChaosPlan,
    ChaosTarget,
    confirmation_text,
    extract_rows,
    ground_plan,
    keyword_plan,
    normalize,
    unknown_refs,
    validate_plan,
)
from understudy.context import Context
from understudy.db import Database
from understudy.events import EventBus
from understudy.fleet.state import FleetState

pytestmark = pytest.mark.anyio

LIVE = {s.id for s in settings.NODES if s.zone == "live"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Storm rows two and three", "storm rows 2 and 3"),
        ("break r1-02's model id", "break r1-02 model id"),
        ("Break R1-02\u2019s model id", "break r1-02 model id"),
        ("fill the disk on rack three-four", "fill the disk on r3-04"),
        ("fill the disk on rack three four", "fill the disk on r3-04"),
        ("fill the disk on rack 3 4", "fill the disk on r3-04"),
        ("fill the disk on rack three, four", "fill the disk on r3-04"),
        ("take down rack one two", "take down r1-02"),
        ("take down rack 1-2", "take down r1-02"),
        ("kill r one oh two", "kill r1-02"),
        ("kill r one zero two", "kill r1-02"),
        ("kill r one dash zero three", "kill r1-03"),
        ("kill r 2 4", "kill r2-04"),
        ("kill three-four", "kill r3-04"),
        ("storm the first row", "storm the row 1"),
        ("storm third row", "storm row 3"),
        ("storm rows two-three", "storm rows 2-3"),
        ("  Surprise   me  ", "surprise me"),
        ("break something", "break something"),
    ],
)
def test_normalize(text, expected):
    assert normalize(text) == expected


def test_normalize_is_idempotent():
    for text in ("fill the disk on rack three-four", "break r1-02's model id", "storm rows two and three"):
        once = normalize(text)
        assert normalize(once) == once


def test_oh_and_zero_only_count_after_a_digit():
    assert normalize("oh break something") == "oh break something"
    assert normalize("zero effort, break something") == "zero effort, break something"


@pytest.mark.parametrize(
    "text, rows",
    [
        ("storm rows 2 and 3", [2, 3]),
        ("storm row 2", [2]),
        ("storm rows 1, 2 and 3", [1, 2, 3]),
        ("storm rows 1-3", [1, 2, 3]),
        ("storm rows 1 through 2", [1, 2]),
        ("storm rows 2 3", [2, 3]),
        ("storm rows 2 and 2", [2]),
        ("storm", []),
        ("disk full on r1-02", []),
    ],
)
def test_extract_rows(text, rows):
    assert extract_rows(text) == rows


def test_unknown_refs():
    assert unknown_refs("break r9-09 and r1-02 and pg-01 and r1-13", LIVE) == ["r9-09", "pg-01", "r1-13"]
    assert unknown_refs("break r1-02", LIVE) == []


def test_validate_plan_accepts_a_clean_plan():
    raw = {"action": "inject", "rows": None, "targets": [{"node_id": "r1-02", "fault_class": "model_id_case"}]}
    plan = validate_plan(raw, LIVE)
    assert plan == ChaosPlan("inject", None, (ChaosTarget("r1-02", "model_id_case"),))
    assert plan.as_dict() == {
        "action": "inject", "rows": None, "targets": [{"node_id": "r1-02", "fault_class": "model_id_case"}],
    }


def test_validate_plan_drops_bad_nodes_and_classes():
    raw = {
        "action": "inject",
        "targets": [
            {"node_id": "r9-09", "fault_class": "disk_full"},
            {"node_id": "pg-01", "fault_class": "disk_full"},
            {"node_id": "r1-01", "fault_class": "gremlins"},
            {"node_id": "r1-02", "fault_class": "Disk Full"},
            {"node_id": "R1-02", "fault_class": "crashed_worker"},
            {"node_id": None, "fault_class": None},
            {"node_id": "r1-03", "fault_class": None},
            {"node_id": "null", "fault_class": "leaky_worker"},
            "r1-04",
        ],
    }
    plan = validate_plan(raw, LIVE)
    assert plan.targets == (
        ChaosTarget("r1-02", "disk_full"),
        ChaosTarget("r1-03", None),
        ChaosTarget(None, "leaky_worker"),
    )


def test_validate_plan_with_nothing_valid_is_none():
    assert validate_plan({"action": "inject", "targets": [{"node_id": "r9-09", "fault_class": "disk_full"}]}, LIVE) is None
    assert validate_plan({"action": "inject", "targets": []}, LIVE) is None
    assert validate_plan({"action": "inject"}, LIVE) is None
    assert validate_plan({"action": None, "rows": None, "targets": []}, LIVE) is None
    assert validate_plan({"action": "explode"}, LIVE) is None
    assert validate_plan(["storm"], LIVE) is None
    assert validate_plan(None, LIVE) is None


def test_validate_plan_rows():
    assert validate_plan({"action": "storm", "rows": [3, 2, 2]}, LIVE).rows == (2, 3)
    assert validate_plan({"action": "storm", "rows": ["1", 2.0]}, LIVE).rows == (1, 2)
    assert validate_plan({"action": "storm", "rows": [0, 2, 4]}, LIVE).rows == (2,)
    assert validate_plan({"action": "storm", "rows": 2}, LIVE).rows == (2,)
    assert validate_plan({"action": "storm", "rows": None}, LIVE).rows is None
    assert validate_plan({"action": "storm", "rows": []}, LIVE).rows is None
    assert validate_plan({"action": "storm", "rows": [4]}, LIVE) is None
    assert validate_plan({"action": "storm", "rows": [0, 7]}, LIVE) is None


def test_validate_plan_caps_targets_and_ignores_targets_for_storm_and_random():
    nodes = sorted(LIVE)[:9]
    raw = {"action": "inject", "targets": [{"node_id": n, "fault_class": "disk_full"} for n in nodes]}
    plan = validate_plan(raw, LIVE)
    assert [t.node_id for t in plan.targets] == nodes[:6]
    storm = validate_plan({"action": "storm", "targets": raw["targets"]}, LIVE)
    assert storm == ChaosPlan("storm") and storm.as_dict()["targets"] == []
    assert validate_plan({"action": "random", "targets": raw["targets"]}, LIVE) == ChaosPlan("random")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("storm", ChaosPlan("storm")),
        ("storm everything", ChaosPlan("storm")),
        ("storm rows 2 and 3", ChaosPlan("storm", (2, 3))),
        ("take down row 2", ChaosPlan("storm", (2,))),
        ("surprise me", ChaosPlan("random")),
        ("break something", ChaosPlan("random")),
        ("break something on row 3", ChaosPlan("random", (3,))),
        ("break r1-02 model id", ChaosPlan("inject", None, (ChaosTarget("r1-02", "model_id_case"),))),
        ("fill the disk on r3-04", ChaosPlan("inject", None, (ChaosTarget("r3-04", "disk_full"),))),
        ("take down r1-02", ChaosPlan("inject", None, (ChaosTarget("r1-02", None),))),
        ("kill the worker on r2-01", ChaosPlan("inject", None, (ChaosTarget("r2-01", "crashed_worker"),))),
        ("make r3-04 forget its api key", ChaosPlan("inject", None, (ChaosTarget("r3-04", "bad_key_env"),))),
        ("squat the port on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "port_squatter"),))),
        ("starve the tokens on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "starved_tokens"),))),
        ("forbidden parameter on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "forbidden_param"),))),
        ("stale endpoint on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "stale_endpoint"),))),
        ("break the config on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "config_syntax"),))),
        ("flaky worker on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "leaky_worker"),))),
        ("log permission on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "log_permission"),))),
        ("upstream storm on r1-01", ChaosPlan("inject", None, (ChaosTarget("r1-01", "upstream_storm"),))),
        ("fill the disk", ChaosPlan("inject", None, (ChaosTarget(None, "disk_full"),))),
        ("break the config on row 1", ChaosPlan("inject", (1,), (ChaosTarget(None, "config_syntax"),))),
        (
            "disk full on r1-01 and bad key on r1-02",
            ChaosPlan("inject", None, (ChaosTarget("r1-01", "disk_full"), ChaosTarget("r1-02", "bad_key_env"))),
        ),
        (
            "kill r1-01 and r1-02",
            ChaosPlan("inject", None, (ChaosTarget("r1-01", "crashed_worker"), ChaosTarget("r1-02", "crashed_worker"))),
        ),
    ],
)
def test_keyword_plan(text, expected):
    assert keyword_plan(normalize(text), LIVE) == expected


@pytest.mark.parametrize("text", ["what is the weather", "hello", "tell me a joke", "", "r9-09 please", "pg-01"])
def test_keyword_plan_finds_nothing_in_gibberish(text):
    assert keyword_plan(normalize(text), LIVE) is None


def test_keyword_plan_ignores_nodes_that_are_not_live():
    assert keyword_plan("break r1-02", {"r1-01"}) is None


def test_ground_plan_rejects_invented_or_missed_nodes_and_rows():
    inject = ChaosPlan("inject", None, (ChaosTarget("r1-01", "config_syntax"),))
    assert ground_plan(inject, "break the config on row 1", LIVE) is None
    assert ground_plan(inject, "break the config on r1-01 and r1-02", LIVE) is None
    assert ground_plan(inject, "break the config on r1-01", LIVE) == inject
    assert ground_plan(ChaosPlan("random"), "break r1-01", LIVE) is None
    stray = ChaosPlan("storm", (1, 2))
    assert ground_plan(stray, "storm everything", LIVE).rows is None
    assert ground_plan(stray, "storm rows 2 and 3", LIVE).rows == (2, 3)
    assert ground_plan(stray, "storm the top row", LIVE).rows == (1, 2)


# execution -------------------------------------------------------------------------


class StubSupervisor:
    def __init__(self, down=()):
        self.specs = {s.id: s for s in settings.NODES}
        self.down = set(down)

    def status(self, node_id):
        return SimpleNamespace(state="exited" if node_id in self.down else "running")


class Narrator:
    def __init__(self):
        self.lines = []

    async def say(self, facts, priority="low", incident_id=None, polish=True, force=False):
        self.lines.append((facts, priority, polish))
        return facts


@pytest.fixture
async def db(tmp_path):
    database = await Database.open(tmp_path / "t.db")
    yield database
    await database.close()


@pytest.fixture
def injections(monkeypatch):
    """Replaces the fault injector with one that only records ground truth."""
    seen: list[tuple[str, str]] = []

    async def fake_inject(sup, node_id, fault_class, seed=None, db=None):
        seen.append((node_id, fault_class))
        truth = await db.add_fault_truth(node_id, fault_class, seed or 0, {}, zone=sup.specs[node_id].zone)
        return SimpleNamespace(node_id=node_id, fault_class=fault_class, seed=seed, truth_id=truth)

    monkeypatch.setattr(chaos_module.faults, "inject", fake_inject)
    return seen


def make_ops(db, chat=None, referee=None, down=(), seed=1):
    import random

    bus = EventBus()
    specs = list(settings.NODES)
    ctx = Context(
        db, bus, StubSupervisor(down), FleetState(specs, bus), referee or FakeReferee(),  # type: ignore[arg-type]
        chat=chat or ScriptedChat(),  # type: ignore[arg-type]
        resolve=lambda key: (key, f"model-{key}"),
    )
    ctx.narrator = Narrator()
    return ChaosOps(ctx, random.Random(seed))


def reply(**plan):
    import json

    return json.dumps({"action": None, "rows": None, "targets": [], **plan})


async def test_gibberish_is_422_not_a_random_fault(db, injections):
    chat = ScriptedChat(chaos=[reply()])
    ops = make_ops(db, chat)
    with pytest.raises(ChaosError) as err:
        await ops.command("what is the weather")
    assert err.value.status == 422 and err.value.message == "could not understand: what is the weather"
    assert injections == []
    assert ops.ctx.narrator.lines == []


async def test_gibberish_with_the_model_down_is_still_422(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=[RuntimeError("boom")]))
    with pytest.raises(ChaosError) as err:
        await ops.command("hello there")
    assert err.value.status == 422 and injections == []


async def test_empty_command_is_422_without_a_model_call(db, injections):
    chat = ScriptedChat()
    with pytest.raises(ChaosError) as err:
        await make_ops(db, chat).command("   ")
    assert err.value.status == 422 and chat.calls == []


async def test_unknown_node_and_unknown_row_are_422(db, injections):
    chat = ScriptedChat()
    ops = make_ops(db, chat)
    with pytest.raises(ChaosError) as err:
        await ops.command("fill the disk on r9-09")
    assert err.value.status == 422 and "r9-09" in err.value.message
    with pytest.raises(ChaosError) as err:
        await ops.command("storm row seven")
    assert err.value.status == 422 and "7" in err.value.message
    with pytest.raises(ChaosError) as err:
        await ops.command("break pg-01")
    assert err.value.status == 422
    assert chat.calls == [] and injections == []


async def test_model_sees_the_normalized_text_and_the_live_nodes(db, injections):
    chat = ScriptedChat(chaos=[reply(action="inject", targets=[{"node_id": "r3-04", "fault_class": "disk_full"}])])
    ops = make_ops(db, chat)
    await ops.command("Fill the disk on rack three-four")
    call = chat.of("chaos")[0]
    assert call["messages"][1] == {"role": "user", "content": "fill the disk on r3-04"}
    system = call["messages"][0]["content"]
    assert "r1-01, r1-02" in system and "pg-01" not in system
    assert "disk_full: " in system and "upstream_storm: " in system
    assert call["thinking"] is False and call["model_key"] == "nano"


async def test_single_node_command(db, injections):
    chat = ScriptedChat(chaos=[reply(action="inject", targets=[{"node_id": "r1-02", "fault_class": "model_id_case"}])])
    ops = make_ops(db, chat)
    body = await ops.command("break r1-02's model id")
    assert body["injected"] == [{"node_id": "r1-02", "fault_class": "model_id_case"}]
    assert body["plan"] == {
        "action": "inject", "rows": None, "targets": [{"node_id": "r1-02", "fault_class": "model_id_case"}],
    }
    assert body["text"] == "Injected model id case on r1-02."
    assert injections == [("r1-02", "model_id_case")]
    assert "skipped" not in body


async def test_confirmation_is_short_and_narrated_without_polish(db, injections):
    chat = ScriptedChat(chaos=[reply(action="storm", rows=[2, 3])])
    ops = make_ops(db, chat)
    body = await ops.command("storm rows two and three")
    line = body["text"]
    assert line.startswith("Storm injected on rows two and three: ") and line.endswith(" faults.")
    assert len(line.split()) <= CONFIRMATION_WORDS
    assert ops.ctx.narrator.lines == [(line, "high", False)]
    rows = {settings.NODES[[n.id for n in settings.NODES].index(i["node_id"])].row for i in body["injected"]}
    assert rows <= {2, 3} and 3 <= len(body["injected"]) <= 6
    assert len({i["node_id"] for i in body["injected"]}) == len(body["injected"])


async def test_storm_without_rows_uses_the_default_pick(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=[reply(action="storm")]))
    body = await ops.command("storm")
    assert body["plan"] == {"action": "storm", "rows": None, "targets": []}
    assert 3 <= len(body["injected"]) <= MAX_DOWN


async def test_surprise_me_injects_one_training_fault(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=[reply(action="random")]))
    body = await ops.command("surprise me")
    assert len(body["injected"]) == 1
    assert body["injected"][0]["fault_class"] in settings.TRAINING_FAULT_CLASSES
    assert body["injected"][0]["node_id"] in LIVE
    assert body["plan"]["action"] == "random"
    assert body["text"].startswith("Injected ")


async def test_surprise_me_works_when_the_model_is_down(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=[RuntimeError("boom")]))
    body = await ops.command("surprise me")
    assert body["plan"]["action"] == "random" and len(body["injected"]) == 1


async def test_budget_exhaustion_falls_back_to_keywords(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=[llm.BudgetExceeded(1.0, 1.0)]))
    body = await ops.command("fill the disk on rack three four")
    assert body["injected"] == [{"node_id": "r3-04", "fault_class": "disk_full"}]


async def test_unusable_model_output_falls_back_to_keywords(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=["I cannot help with that."]))
    body = await ops.command("make r3-04 forget its API key")
    assert body["injected"] == [{"node_id": "r3-04", "fault_class": "bad_key_env"}]


async def test_invented_node_in_model_output_is_discarded(db, injections):
    chat = ScriptedChat(chaos=[reply(action="inject", rows=[1], targets=[{"node_id": "r1-01", "fault_class": "config_syntax"}])])
    ops = make_ops(db, chat)
    body = await ops.command("break the config on row one")
    assert body["plan"]["targets"] == [{"node_id": None, "fault_class": "config_syntax"}]
    assert body["plan"]["rows"] == [1]
    assert body["injected"][0]["node_id"].startswith("r1-")


async def test_named_node_without_a_class_gets_a_random_training_class(db, injections):
    chat = ScriptedChat(chaos=[reply(action="inject", targets=[{"node_id": "r1-02", "fault_class": None}])])
    body = await make_ops(db, chat).command("take down rack one two")
    assert body["injected"][0]["node_id"] == "r1-02"
    assert body["injected"][0]["fault_class"] in settings.TRAINING_FAULT_CLASSES


async def test_named_class_without_a_node_picks_a_free_node(db, injections):
    chat = ScriptedChat(chaos=[reply(action="inject", rows=[2], targets=[{"node_id": None, "fault_class": "disk_full"}])])
    body = await make_ops(db, chat).command("fill the disk on row two")
    node = body["injected"][0]["node_id"]
    assert node.startswith("r2-") and body["injected"][0]["fault_class"] == "disk_full"


async def test_held_out_classes_can_be_requested(db, injections):
    chat = ScriptedChat(chaos=[reply(action="inject", targets=[{"node_id": "r1-01", "fault_class": "upstream_storm"}])])
    body = await make_ops(db, chat).command("upstream storm on r1-01")
    assert body["injected"] == [{"node_id": "r1-01", "fault_class": "upstream_storm"}]


async def test_partial_failure_reports_what_worked_and_what_was_skipped(db, injections):
    chat = ScriptedChat(chaos=[reply(
        action="inject",
        targets=[{"node_id": "r1-01", "fault_class": "disk_full"}, {"node_id": "r1-02", "fault_class": "bad_key_env"}],
    )])
    ops = make_ops(db, chat)
    ops.ctx.state.set_status("r1-02", "down")
    body = await ops.command("disk full on r1-01 and bad key on r1-02")
    assert body["injected"] == [{"node_id": "r1-01", "fault_class": "disk_full"}]
    assert body["skipped"] == [
        {"node_id": "r1-02", "fault_class": "bad_key_env", "reason": "r1-02 is not a healthy live node"}
    ]
    assert body["text"] == "Injected disk full on r1-01. Skipped r1-02."


async def test_nothing_injected_is_a_409_with_the_reason(db, injections):
    chat = ScriptedChat(chaos=[reply(action="inject", targets=[{"node_id": "r1-02", "fault_class": "disk_full"}])])
    ops = make_ops(db, chat)
    ops.ctx.state.set_status("r1-02", "down")
    with pytest.raises(ChaosError) as err:
        await ops.command("fill the disk on r1-02")
    assert err.value.status == 409 and err.value.message == "r1-02 is not a healthy live node"
    assert ops.ctx.narrator.lines == []


async def test_no_room_is_a_409_for_every_kind_of_command(db, injections):
    for node in sorted(LIVE)[:MAX_DOWN]:
        await db.add_fault_truth(node, "disk_full", 1)
    chat = ScriptedChat(chaos=[
        reply(action="storm"),
        reply(action="random"),
        reply(action="inject", targets=[{"node_id": "r3-04", "fault_class": "disk_full"}]),
    ])
    ops = make_ops(db, chat)
    for text in ("storm", "surprise me", "fill the disk on r3-04"):
        with pytest.raises(ChaosError) as err:
            await ops.command(text)
        assert err.value.status == 409 and err.value.message == f"no room: {MAX_DOWN} nodes are down or faulty"
    assert injections == []


async def test_room_is_rechecked_between_targets(db, injections):
    for node in sorted(LIVE)[: MAX_DOWN - 1]:
        await db.add_fault_truth(node, "disk_full", 1)
    free = sorted(LIVE)[MAX_DOWN:]
    chat = ScriptedChat(chaos=[reply(
        action="inject", targets=[{"node_id": free[0], "fault_class": "disk_full"}, {"node_id": free[1], "fault_class": "disk_full"}],
    )])
    body = await make_ops(db, chat).command(f"disk full on {free[0]} and {free[1]}")
    assert [i["node_id"] for i in body["injected"]] == [free[0]]
    assert body["skipped"][0]["node_id"] == free[1] and body["skipped"][0]["reason"].startswith("no room")


async def test_chaos_agent_is_announced_and_finished(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=[reply(action="random")]))
    drain = collect(ops.ctx.bus)
    await ops.command("surprise me")
    events = drain()
    spawned = [e.data for e in events if e.type == "agent_spawned"]
    assert [(e["role"], e["rung"]) for e in spawned] == [("chaos", "nano")]
    assert [e.data["agent_id"] for e in events if e.type == "agent_done"] == [spawned[0]["agent_id"]]


async def test_agent_finishes_when_the_command_fails(db, injections):
    ops = make_ops(db, ScriptedChat(chaos=[reply()]))
    drain = collect(ops.ctx.bus)
    with pytest.raises(ChaosError):
        await ops.command("what is the weather")
    assert sum(e.type == "agent_done" for e in drain()) == 1


@pytest.mark.parametrize(
    "plan, injected, skipped",
    [
        (ChaosPlan("storm"), [{"node_id": f"r1-0{i}", "fault_class": "disk_full"} for i in range(1, 5)]
         + [{"node_id": "r2-01", "fault_class": "disk_full"}, {"node_id": "r3-01", "fault_class": "disk_full"}], []),
        (ChaosPlan("storm"), [{"node_id": "r1-01", "fault_class": "disk_full"}], []),
        (ChaosPlan("inject"), [{"node_id": f"r{r}-0{c}", "fault_class": "starved_tokens"} for r in (1, 2) for c in (1, 2, 3)], []),
        (ChaosPlan("inject"), [{"node_id": f"r{r}-0{c}", "fault_class": "starved_tokens"} for r in (1, 2) for c in (1, 2, 3)],
         [{"node_id": "r3-01"}, {"node_id": "r3-02"}, {"node_id": None}]),
        (ChaosPlan("inject"), [{"node_id": "r1-01", "fault_class": "log_permission"}], [{"node_id": "r1-02"}]),
    ],
)
def test_confirmation_text_stays_short(plan, injected, skipped):
    rows = {s.id: s.row for s in settings.NODES}
    line = confirmation_text(plan, injected, skipped, rows)
    assert line.endswith(".") and len(line.split()) <= CONFIRMATION_WORDS


def test_confirmation_text_examples():
    rows = {s.id: s.row for s in settings.NODES}
    storm = [{"node_id": n, "fault_class": "disk_full"} for n in ("r2-01", "r2-02", "r3-01", "r3-02", "r3-03")]
    assert confirmation_text(ChaosPlan("storm"), storm, [], rows) == "Storm injected on rows two and three: five faults."
    one = [{"node_id": "r1-01", "fault_class": "disk_full"}]
    assert confirmation_text(ChaosPlan("storm"), one, [], rows) == "Storm injected on row one: one fault."
    assert confirmation_text(ChaosPlan("inject"), one, [], rows) == "Injected disk full on r1-01."


# storm through the ops API ---------------------------------------------------------


async def test_storm_with_no_room_raises_409(db, injections):
    for node in sorted(LIVE)[:MAX_DOWN]:
        await db.add_fault_truth(node, "disk_full", 1)
    with pytest.raises(ChaosError) as err:
        await make_ops(db).storm()
    assert err.value.status == 409 and err.value.message == f"no room: {MAX_DOWN} nodes are down or faulty"


async def test_storm_with_no_free_nodes_in_the_rows_raises_409(db, injections):
    for node in ("r1-01", "r1-02", "r1-03", "r1-04"):
        await db.add_fault_truth(node, "disk_full", 1)
    with pytest.raises(ChaosError) as err:
        await make_ops(db).storm(rows=[1])
    assert err.value.status == 409 and err.value.message == "no free healthy nodes in row one"


async def test_storm_plants_faults_when_there_is_room(db, injections):
    planted = await make_ops(db).storm(rows=[2], count=3)
    assert len(planted) == 3 and all(p["node_id"].startswith("r2-") for p in planted)


# stale fault sweep -----------------------------------------------------------------


async def stale_fault(db, node, age=600.0):
    await db.add_fault_truth(node, "leaky_worker", 1)
    await db.execute("UPDATE faults_truth SET injected_at = ? WHERE node_id = ?", (time.time() - age, node))


async def test_sweep_clears_a_healthy_node_that_passes_the_referee(db):
    referee = FakeReferee([True])
    ops = make_ops(db, referee=referee)
    await stale_fault(db, "r1-01")
    resets = []
    ops.ctx.monitor = SimpleNamespace(reset=resets.append)
    assert await ops.sweep_stale_faults() == "r1-01"
    assert referee.verified == ["r1-01"] and resets == ["r1-01"]
    assert await db.active_faults() == []
    assert not ops._busy


async def test_sweep_keeps_the_row_when_verification_fails(db):
    referee = FakeReferee([False])
    ops = make_ops(db, referee=referee)
    await stale_fault(db, "r1-01")
    assert await ops.sweep_stale_faults() is None
    assert referee.verified == ["r1-01"]
    assert [r["node_id"] for r in await db.active_faults()] == ["r1-01"]
    assert not ops._busy


async def test_sweep_skips_nodes_that_are_not_idle(db):
    referee = FakeReferee()
    ops = make_ops(db, referee=referee)
    for node in ("r1-01", "r1-02", "r1-03", "r1-04", "r2-01"):
        await stale_fault(db, node)
    await db.open_incident("r1-01", "probe failed", "low")
    ops.ctx.state.set_status("r1-02", "repairing")
    ops.ctx.state.set_status("r1-03", "down")
    ops._busy.add("r1-04")
    await stale_fault(db, "r2-02", age=5.0)
    await stale_fault(db, "pg-01")
    assert await ops.sweep_stale_faults() == "r2-01"
    assert referee.verified == ["r2-01"]
    assert sorted(r["node_id"] for r in await db.active_faults()) == [
        "pg-01", "r1-01", "r1-02", "r1-03", "r1-04", "r2-02",
    ]


async def test_sweep_verifies_one_node_per_call_and_frees_room(db, injections):
    referee = FakeReferee([True, True])
    ops = make_ops(db, referee=referee)
    for node in sorted(LIVE)[:MAX_DOWN]:
        await stale_fault(db, node)
    assert await ops._held_back() is True
    assert await ops.sweep_stale_faults() == "r1-01"
    assert await ops.sweep_stale_faults() == "r1-02"
    assert referee.verified == ["r1-01", "r1-02"]
    assert await ops._held_back() is False
    planted = await ops.storm(rows=[3], count=3)
    assert len(planted) == MAX_DOWN - 4


async def test_sweep_does_not_clear_when_the_node_is_taken_during_verification(db):
    class Racing(FakeReferee):
        async def verify(self, node_id):
            await db.open_incident(node_id, "probe failed", "low")
            return await super().verify(node_id)

    ops = make_ops(db, referee=Racing([True]))
    await stale_fault(db, "r1-01")
    assert await ops.sweep_stale_faults() is None
    assert [r["node_id"] for r in await db.active_faults()] == ["r1-01"]


async def test_sweep_with_nothing_to_do_does_not_probe(db):
    referee = FakeReferee()
    ops = make_ops(db, referee=referee)
    assert await ops.sweep_stale_faults() is None
    assert referee.verified == []
