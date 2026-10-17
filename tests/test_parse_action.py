import pytest

from understudy.llm import parse_action

ACTION = {"thought": "check the log", "action": "tail_log", "args": {"n": 40}}


def test_plain_json():
    assert parse_action('{"thought": "check the log", "action": "tail_log", "args": {"n": 40}}') == ACTION


def test_code_fence_with_language():
    text = '```json\n{"thought": "check the log", "action": "tail_log", "args": {"n": 40}}\n```'
    assert parse_action(text) == ACTION


def test_code_fence_without_language_and_prose_around():
    text = 'Sure, here is my next step:\n```\n{"thought": "check the log", "action": "tail_log", "args": {"n": 40}}\n```\nLet me know.'
    assert parse_action(text) == ACTION


def test_trailing_commas():
    assert parse_action('{"thought": "check the log", "action": "tail_log", "args": {"n": 40,},}') == ACTION
    assert parse_action('{"action": "x", "args": {"paths": ["a", "b",]}}') == {"action": "x", "args": {"paths": ["a", "b"]}}


def test_single_quotes():
    assert parse_action("{'thought': 'check the log', 'action': 'tail_log', 'args': {'n': 40}}") == ACTION


def test_single_quotes_with_apostrophe_and_double_quote_inside():
    text = r"{'thought': 'it\'s a \"quoted\" word', 'action': 'node_status', 'args': {}}"
    assert parse_action(text) == {"thought": 'it\'s a "quoted" word', "action": "node_status", "args": {}}


def test_python_literals_and_bare_keys():
    assert parse_action("{action: 'claim_fixed', args: {'ok': True, 'note': None, 'bad': False}}") == {
        "action": "claim_fixed",
        "args": {"ok": True, "note": None, "bad": False},
    }


def test_all_three_deviations_together():
    text = "```json\n{'thought': 'x', 'action': 'restart_node', 'args': {},}\n```"
    assert parse_action(text) == {"thought": "x", "action": "restart_node", "args": {}}


def test_braces_inside_strings_do_not_confuse_balancing():
    text = '{"thought": "use {x} and } carefully", "action": "write_config", "args": {"yaml_text": "a: {b: 1}"}}'
    parsed = parse_action(text)
    assert parsed["thought"] == "use {x} and } carefully"
    assert parsed["args"]["yaml_text"] == "a: {b: 1}"


def test_first_object_wins():
    text = '{"action": "first", "args": {}} then {"action": "second", "args": {}}'
    assert parse_action(text)["action"] == "first"


def test_raw_newlines_inside_strings():
    parsed = parse_action('{"action": "write_config", "args": {"yaml_text": "port: 9200\nmodel: x\n"}}')
    assert parsed["args"]["yaml_text"] == "port: 9200\nmodel: x\n"


def test_escaped_newlines_round_trip():
    parsed = parse_action(r'{"action": "write_config", "args": {"yaml_text": "port: 9200\nmodel: x\n"}}')
    assert parsed["args"]["yaml_text"] == "port: 9200\nmodel: x\n"


def test_think_block_is_ignored():
    text = '<think>maybe {"action": "wrong"} instead</think>{"action": "right", "args": {}}'
    assert parse_action(text) == {"action": "right", "args": {}}


def test_dangling_close_think_tag():
    assert parse_action('I could {try} this</think>\n{"action": "right", "args": {}}') == {"action": "right", "args": {}}


def test_malformed_first_object_falls_through_to_the_next():
    assert parse_action('{oops this is not json} {"action": "ok", "args": {}}') == {"action": "ok", "args": {}}


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "no json here",
        "[1, 2, 3]",
        '{"action": "tail_log", "args": {',
        "{not: valid: at all}",
    ],
)
def test_returns_none_on_failure(text):
    assert parse_action(text) is None


def test_non_object_json_is_not_returned():
    assert parse_action('["action"]') is None
