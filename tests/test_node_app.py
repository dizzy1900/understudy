import asyncio
import os
import signal

import httpx
import pytest

from tests.fleet_support import FakeUpstream, chat_body, free_port, make_supervisor, post_chat
from understudy.fleet.nodeconfig import dump_config, load_config_dict

pytestmark = pytest.mark.anyio


@pytest.fixture
def upstream():
    server = FakeUpstream().start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def test_key(monkeypatch):
    monkeypatch.setenv("CRUSOE_API_KEY", "test-key")


@pytest.fixture
def sup(tmp_path, upstream):
    supervisor = make_supervisor(tmp_path, 1, upstream.base_url)
    yield supervisor
    for proc in list(supervisor._procs.values()):
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def edit(sup, node_id, **changes):
    paths = sup.paths(node_id)
    cfg = load_config_dict(paths.config)
    cfg.update(changes)
    paths.config.write_text(dump_config(cfg))


def log_text(sup, node_id):
    return sup.paths(node_id).log.read_text()


async def test_proxy_forwards_with_config_overrides(sup, upstream):
    node = "t-01"
    status = await sup.start(node)
    assert status.state == "running"
    async with httpx.AsyncClient() as client:
        body = chat_body() | {"model": "something/else", "max_tokens": 5000}
        reply = await client.post(f"http://127.0.0.1:{status.port}/v1/chat/completions", json=body)
    assert reply.status_code == 200
    assert reply.json()["choices"][0]["message"]["content"] == "OK"
    seen = upstream.requests[-1]
    assert seen["body"]["model"] == "test/Canonical-Model-1B"
    assert seen["body"]["max_tokens"] == 256
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["auth"] == "Bearer test-key"
    line = [ln for ln in log_text(sup, node).splitlines() if "req id=" in ln][-1]
    for part in ("status=200", "latency_ms=", "total_tokens=12", "finish=stop"):
        assert part in line


async def test_upstream_error_passes_through_and_is_logged(sup):
    node = "t-01"
    edit(sup, node, model="test/canonical-model-1b")
    status = await sup.start(node)
    async with httpx.AsyncClient() as client:
        reply = await post_chat(client, status.port)
    assert reply.status_code == 404
    assert reply.json()["code"] == "model_not_found"
    assert 'upstream_error="Model not found: test/canonical-model-1b"' in log_text(sup, node)


async def test_forbidden_parameter_is_forwarded_and_rejected(sup):
    node = "t-01"
    edit(sup, node, extra_body={"enable_thinking": True})
    status = await sup.start(node)
    async with httpx.AsyncClient() as client:
        reply = await post_chat(client, status.port)
    assert reply.status_code == 403
    assert "parameter 'enable_thinking' is not allowed" in log_text(sup, node)


async def test_missing_key_env_means_no_credentials(sup):
    node = "t-01"
    edit(sup, node, api_key_env="NOT_A_REAL_ENV_VAR")
    status = await sup.start(node)
    async with httpx.AsyncClient() as client:
        reply = await post_chat(client, status.port)
    assert reply.status_code == 401
    assert "NOT_A_REAL_ENV_VAR is not set" in log_text(sup, node)


async def test_unreachable_upstream_returns_502(sup):
    node = "t-01"
    edit(sup, node, upstream_base_url=f"http://127.0.0.1:{free_port()}/v1")
    status = await sup.start(node)
    async with httpx.AsyncClient() as client:
        reply = await post_chat(client, status.port)
    assert reply.status_code == 502
    assert "upstream connection failed" in log_text(sup, node)


async def test_empty_content_is_logged(sup):
    node = "t-01"
    edit(sup, node, extra_body={"chat_template_kwargs": {"enable_thinking": True}}, max_tokens=16)
    status = await sup.start(node)
    async with httpx.AsyncClient() as client:
        reply = await post_chat(client, status.port)
    assert reply.status_code == 200
    assert reply.json()["choices"][0]["finish_reason"] == "length"
    assert "upstream returned empty content finish=length" in log_text(sup, node)


async def test_config_parse_error_exits_2_with_line_number(sup):
    node = "t-01"
    paths = sup.paths(node)
    paths.config.write_text(paths.config.read_text().replace("max_tokens: 256", "max_tokens: [256"))
    status = await sup.start(node)
    assert status.state == "exited"
    assert status.exit_code == 2
    text = log_text(sup, node)
    assert "config parse error line " in text
    line_no = int(text.split("config parse error line ")[1].split(":")[0])
    assert line_no >= 1


async def test_unwritable_log_exits_3_and_reports_on_stderr(sup):
    if os.geteuid() == 0:
        pytest.skip("chmod does not restrict root")
    node = "t-01"
    paths = sup.ensure_node(node)
    paths.log.write_text("")
    os.chmod(paths.log, 0o000)
    try:
        status = await sup.start(node)
        assert status.state == "exited"
        assert status.exit_code == 3
        assert any("Permission denied" in line for line in sup.stderr_tail(node))
    finally:
        os.chmod(paths.log, 0o644)


async def test_bind_failure_exits_4(sup):
    node = "t-01"
    pid = await sup.spawn_squatter(node)
    try:
        status = await sup.start(node)
        assert status.state == "exited"
        assert status.exit_code == 4
        assert "bind failed: address in use" in log_text(sup, node)
        owner = sup.port_owner(sup.port_of(node))
        assert owner is not None and owner["pid"] == pid and owner["fleet_tracked"]
    finally:
        assert await sup.kill_squatter(pid)
    assert sup.port_owner(sup.port_of(node)) is None
    status = await sup.start(node)
    assert status.state == "running"


async def test_kill_squatter_refuses_untracked_pid(sup):
    assert not await sup.kill_squatter(os.getpid())


async def test_quota_exceeded_answers_507_until_files_are_removed(sup):
    node = "t-01"
    paths = sup.ensure_node(node)
    edit(sup, node, quota_mb=1)
    for i in range(3):
        (paths.data / f"spool-{i}.bin").write_bytes(b"x" * 600_000)
    status = await sup.start(node)
    assert status.state == "running"
    async with httpx.AsyncClient() as client:
        reply = await post_chat(client, status.port)
        assert reply.status_code == 507
        assert "quota exceeded" in reply.text
        for path in paths.data.glob("spool-*.bin"):
            path.unlink()
        await asyncio.sleep(1.2)
        assert (await post_chat(client, status.port)).status_code == 200
    text = log_text(sup, node)
    assert "data dir over quota" in text
    assert "data dir back under quota" in text


async def test_checkpoint_written_at_boot_and_stats(sup):
    node = "t-01"
    status = await sup.start(node)
    async with httpx.AsyncClient() as client:
        await post_chat(client, status.port)
        stats = (await client.get(f"http://127.0.0.1:{status.port}/local/stats")).json()
    assert stats["tokens_served"] == 12
    assert stats["requests_total"] == 1 and stats["requests_ok"] == 1
    assert stats["last_checkpoint"]
    files = list(sup.paths(node).data.glob("ckpt-*.bin"))
    assert len(files) == 1
    assert files[0].stat().st_size == 200 * 1024


async def test_worker_recycle_exits_after_n_requests(sup):
    node = "t-01"
    edit(sup, node, worker_recycle_after=3)
    status = await sup.start(node)
    async with httpx.AsyncClient() as client:
        for _ in range(3):
            assert (await post_chat(client, status.port)).status_code == 200
    for _ in range(50):
        if sup.status(node).state == "exited":
            break
        await asyncio.sleep(0.1)
    final = sup.status(node)
    assert final.state == "exited"
    assert final.exit_code == 1
    assert "worker recycled after 3 requests" in log_text(sup, node)


async def test_concurrency_above_limit_without_backoff_triggers_429_storm(tmp_path):
    slow = FakeUpstream(delay=0.4).start()
    try:
        sup = make_supervisor(tmp_path, 1, slow.base_url)
        node = "t-01"
        edit(sup, node, worker_concurrency=8, upstream_limit=2, retry_backoff=False)
        status = await sup.start(node)
        async with httpx.AsyncClient() as client:
            replies = await asyncio.gather(*(post_chat(client, status.port) for _ in range(6)))
            codes = sorted(r.status_code for r in replies)
            assert codes.count(200) == 2 and codes.count(429) == 4
            storm = next(r for r in replies if r.status_code == 429)
            assert storm.json()["code"] == "rate_limit_exceeded"
            assert storm.headers["retry-after"]
            # The upstream keeps refusing for a cooldown window, so a lone request fails too.
            assert (await post_chat(client, status.port)).status_code == 429
        assert slow.max_in_flight == 2
        assert "concurrent request limit exceeded" in log_text(sup, node)
        await sup.stop(node)
    finally:
        slow.stop()


async def test_concurrency_above_limit_with_backoff_queues_instead(tmp_path):
    slow = FakeUpstream(delay=0.3).start()
    try:
        sup = make_supervisor(tmp_path, 1, slow.base_url)
        node = "t-01"
        edit(sup, node, worker_concurrency=8, upstream_limit=2, retry_backoff=True)
        status = await sup.start(node)
        async with httpx.AsyncClient() as client:
            replies = await asyncio.gather(*(post_chat(client, status.port) for _ in range(4)))
        assert [r.status_code for r in replies] == [200] * 4
        assert slow.max_in_flight <= 2
        await sup.stop(node)
    finally:
        slow.stop()


async def test_supervisor_lifecycle(sup):
    node = "t-01"
    assert sup.status(node).state == "new"
    started = await sup.start(node)
    assert started.state == "running" and started.pid
    first_pid = started.pid
    restarted = await sup.restart(node)
    assert restarted.state == "running" and restarted.pid != first_pid
    killed = await sup.kill(node, signal.SIGKILL)
    assert killed.state == "exited" and killed.exit_code == -9
    stopped_again = await sup.stop(node)
    assert stopped_again.state == "exited"
    await sup.start(node)
    stopped = await sup.stop(node)
    assert stopped.state == "exited"
    assert stopped.exit_code in (-15, 0)
    assert stopped.restarts == 3


async def test_reset_restores_canonical_state(sup):
    node = "t-01"
    paths = sup.ensure_node(node)
    edit(sup, node, model="wrong/model", worker_recycle_after=2)
    (paths.data / "spool-1.bin").write_bytes(b"x" * 1000)
    status = await sup.reset(node)
    assert status.state == "running"
    cfg = load_config_dict(paths.config)
    assert cfg["worker_recycle_after"] == 0
    assert cfg["model"] == "test/Canonical-Model-1B"
    assert cfg["upstream_base_url"].startswith("https://api.inference.crusoecloud.com")
    assert not list(paths.data.glob("spool-*"))
