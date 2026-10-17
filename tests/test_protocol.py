import pytest

from understudy.agents import protocol
from understudy.agents.watchdog import Watchdog


@pytest.mark.parametrize(
    "text",
    [
        '{"thought": "look", "action": "tail_log", "args": {"n": 20}}',
        '```json\n{"thought": "look", "action": "tail_log", "args": {"n": 20}}\n```',
        'Sure. {"thought": "look", "action": "tail_log", "args": {"n": 20},}',
        "<think>hmm</think>{'thought': 'look', 'action': 'tail_log', 'args': {'n': 20}}",
        '{"thought": "look", "tool": "tail_log", "args": "{\\"n\\": 20}"}',
        '{"thought": "look", "action": "tail_log", "n": 20}',
    ],
)
def test_parse_turn_accepts_variants(text):
    parsed = protocol.parse_turn(text)
    assert parsed is not None and parsed.action == "tail_log" and parsed.args == {"n": 20}


@pytest.mark.parametrize("text", ["", "no json here", '{"thought": "x"}', '{"action": 3}', '["tail_log"]'])
def test_parse_turn_rejects_garbage(text):
    assert protocol.parse_turn(text) is None


def test_prefix_is_byte_stable_until_a_card_changes():
    card = {"fault_class": "port_squatter", "version": 1, "card": {"signature": "bind failed", "fix_steps": ["a", "b"]}}
    other = {"fault_class": "disk_full", "version": 2, "card": {"signature": "507"}}
    first = protocol.build_messages([card, other], "Incident 1")
    again = protocol.build_messages([other, card], "Incident 2 on another node")
    assert first[0] == again[0] and first[1] == again[1]
    assert [m["role"] for m in first] == ["system", "system", "user"]
    changed = protocol.build_messages([card, {**other, "version": 3}], "Incident 1")
    assert changed[0] == first[0] and changed[1] != first[1]


def test_system_prompt_lists_every_tool_and_no_timestamps():
    prompt = protocol.system_prompt()
    for name in protocol.TOOL_NAMES:
        assert f"- {name}(" in prompt
    assert prompt == protocol.system_prompt()


def test_watchdog_trips_on_repeats_budget_and_stall():
    now = [0.0]
    dog = Watchdog(max_steps=4, stall_s=90, clock=lambda: now[0])
    assert dog.record("node_status", {}) is None
    assert dog.record("tail_log", {"n": 40}) is None
    assert dog.record("node_status", {}) is None
    assert dog.record("node_status", {}) is not None
    assert dog.before_step() is not None
    other = Watchdog(max_steps=14, stall_s=90, clock=lambda: now[0])
    assert other.stalled() is None
    now[0] = 91.0
    assert other.stalled() is not None
    other.record("node_status", {})
    assert other.stalled() is None


def test_watchdog_does_not_count_different_arguments():
    dog = Watchdog()
    assert all(dog.record("tail_log", {"n": n}) is None for n in (10, 20, 30, 40))


