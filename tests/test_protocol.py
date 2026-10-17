import pytest

from understudy.agents import protocol
from understudy.agents.watchdog import Watchdog
from understudy.agents.promoter import start_rung
from understudy.agents.script_child import ScriptRejected, check_source


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
    changed = protocol.build_messages([card, {**other, "version": 3, "card": {"signature": "507 quota exceeded"}}], "Incident 1")
    assert changed[0] == first[0] and changed[1] != first[1]
    assert protocol.build_messages([card, {**other, "version": 9}], "x")[1] == first[1], "a version bump alone must not change the block"


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


GOOD = '''
import yaml

def fix(tools):
    text = tools.read_config().text
    cfg = yaml.safe_load(text)
    cfg["max_tokens"] = 300
    done = tools.write_config(yaml_text=yaml.safe_dump(cfg))
    if not done.ok:
        raise Exception("write failed: " + done.text)
    return "set max_tokens to 300", "max_tokens was below the standard value"
'''


@pytest.mark.parametrize(
    "source, message",
    [
        ("import os\ndef fix(tools):\n    return 'a', 'b'", "import of 'os'"),
        ("from os import path\ndef fix(tools):\n    return 'a', 'b'", "import from 'os'"),
        ("def fix(tools):\n    return open('/etc/passwd').read(), 'b'", "'open'"),
        ("def fix(tools):\n    return getattr(tools, 'x'), 'b'", "'getattr'"),
        ("def fix(tools):\n    return tools.__class__, 'b'", "private attribute"),
        ("import yaml\ndef fix(tools):\n    return yaml.load('a: 1'), 'b'", "yaml.safe_load"),
        ("class A:\n    pass\ndef fix(tools):\n    return 'a', 'b'", "ClassDef"),
        ("x = 1", "must define fix"),
        ("def fix(tools:\n    pass", "syntax error"),
    ],
)
def test_static_check_rejects_unsafe_or_malformed_scripts(source, message):
    with pytest.raises(ScriptRejected, match=message):
        check_source(source)


def test_static_check_accepts_a_script_using_re_json_yaml():
    check_source(GOOD)
    check_source("import re, json\ndef fix(tools):\n    return json.dumps(re.findall('a', 'aa')), 'x'")


def test_start_rung_walks_back_through_escalations():
    assert start_rung("super", 1) == "lightning" and start_rung("super", 2) == "script"
    assert start_rung("lightning", 0) == "lightning" and start_rung("script", 3) == "script"
