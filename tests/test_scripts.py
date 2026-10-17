import pytest

from tests.agent_support import NODE, OPEN, FakeReferee, ScriptedChat, make_context
from understudy.agents.responder import Responder
from understudy.agents.script_runner import run_script
from understudy.agents.tools import ToolBox

pytestmark = pytest.mark.anyio

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


@pytest.fixture(autouse=True)
async def close_databases():
    yield
    while OPEN:
        await OPEN.pop().close()


async def toolbox(tmp_path):
    ctx = await make_context(tmp_path, ScriptedChat())
    iid = await ctx.db.open_incident(NODE, "probe failed", "low")
    return ctx, ToolBox(ctx, iid, NODE, "responder-1"), iid


def script(tmp_path, body, name="model_id_case.py"):
    path = tmp_path / name
    path.write_text(body)
    return path


async def test_script_reaches_the_node_only_through_the_tool_layer(tmp_path):
    ctx, tools, iid = await toolbox(tmp_path)
    result = await run_script(tools, script(tmp_path, GOOD))
    assert result.ok and result.summary == "set max_tokens to 300" and result.calls == 2
    assert "max_tokens: 300" in tools.paths.config.read_text()
    assert [a["tool"] for a in await ctx.db.actions_for(iid)] == ["read_config", "write_config"]


async def test_script_can_use_the_allowed_modules_without_importing_them(tmp_path):
    ctx, tools, iid = await toolbox(tmp_path)
    body = GOOD.replace("import yaml\n", "")
    result = await run_script(tools, script(tmp_path, body))
    assert result.ok and result.calls == 2


async def test_script_cannot_claim_or_use_unknown_tools_and_jail_applies(tmp_path):
    ctx, tools, iid = await toolbox(tmp_path)
    body = (
        "def fix(tools):\n"
        "    a = tools.claim_fixed(summary='x', root_cause='y')\n"
        "    b = tools.rm_rf()\n"
        "    c = tools.list_dir(rel='../..')\n"
        "    if a.ok or b.ok or c.ok:\n"
        "        raise Exception('a refused call succeeded')\n"
        "    return 'checked', 'the tool layer refuses these calls'\n"
    )
    result = await run_script(tools, script(tmp_path, body))
    assert result.ok, result.error
    assert [a["tool"] for a in await ctx.db.actions_for(iid)] == ["list_dir"]


async def test_script_errors_timeouts_and_rejected_source_are_reported(tmp_path):
    ctx, tools, _ = await toolbox(tmp_path)
    failed = await run_script(tools, script(tmp_path, "def fix(tools):\n    raise ValueError('wrong state')"))
    assert not failed.ok and "ValueError: wrong state" in failed.error
    loop = await run_script(tools, script(tmp_path, "def fix(tools):\n    while True:\n        pass", "loop.py"), timeout=1.5)
    assert not loop.ok and "did not finish" in loop.error
    bad = await run_script(tools, script(tmp_path, "import os\ndef fix(tools):\n    return 'a', 'b'", "bad.py"))
    assert not bad.ok and "not allowed" in bad.error
    empty = await run_script(tools, script(tmp_path, "def fix(tools):\n    return 'done', ''", "empty.py"))
    assert not empty.ok and "two non-empty strings" in empty.error


async def test_script_runs_without_the_api_key_in_its_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CRUSOE_API_KEY", "sk-test-secret")
    ctx, tools, _ = await toolbox(tmp_path)
    body = "def fix(tools):\n    return tools.node_status().text[:20], 'ok'\n"
    assert (await run_script(tools, script(tmp_path, body))).ok


async def test_script_rung_runs_the_script_and_the_referee_decides(tmp_path):
    ctx = await make_context(tmp_path, ScriptedChat(), scripts_dir=tmp_path / "scripts")
    row = await ctx.db.get_incident(await ctx.db.open_incident(NODE, "probe failed: HTTP 404", "low"))
    out = await Responder(ctx, row, "script", "model_id_case").run()
    assert out.status == "escalate" and "no compiled script" in out.reason
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "model_id_case.py").write_text(GOOD)
    chat = ScriptedChat()
    ctx = await make_context(tmp_path / "two", chat, FakeReferee([True]), scripts_dir=tmp_path / "scripts")
    row = await ctx.db.get_incident(await ctx.db.open_incident(NODE, "probe failed: HTTP 404", "low"))
    out = await Responder(ctx, row, "script", "model_id_case").run()
    assert out.status == "verified" and out.quality is None and not chat.calls
    tools = [a["tool"] for a in await ctx.db.actions_for(row["id"])]
    assert tools == ["read_config", "write_config", "run_script"]
    rejected = await make_context(tmp_path / "three", ScriptedChat(), FakeReferee([False]), scripts_dir=tmp_path / "scripts")
    row = await rejected.db.get_incident(await rejected.db.open_incident(NODE, "probe failed", "low"))
    out = await Responder(rejected, row, "script", "model_id_case").run()
    assert out.status == "escalate" and "referee rejected the script" in out.reason
