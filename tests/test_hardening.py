"""Input validation, CORS configuration and the read-only API's failure modes."""
from __future__ import annotations

import importlib

import pytest
from starlette.testclient import TestClient

from config import settings
from tests.test_server import served  # noqa: F401  (fixture)
from understudy import server


def test_cors_origins_parse_env_and_keep_dev_defaults():
    assert settings.cors_origins("") == list(settings.DEV_CORS_ORIGINS)
    got = settings.cors_origins(" https://demo.example.com/ , *, ftp://x, https://demo.example.com,http://localhost:5173")
    assert got == [*settings.DEV_CORS_ORIGINS, "https://demo.example.com"]


def test_cors_env_reaches_the_live_server(monkeypatch):
    monkeypatch.setenv("UNDERSTUDY_CORS_ORIGINS", "https://understudy.example.com")
    from types import SimpleNamespace

    with TestClient(server.create_app(SimpleNamespace(promoter=None), web_dist=None)) as client:
        reply = client.get("/healthz", headers={"Origin": "https://understudy.example.com"})
        assert reply.headers["access-control-allow-origin"] == "https://understudy.example.com"
        evil = client.get("/healthz", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in evil.headers
        assert reply.json()["ok"] is True and "db" in reply.json()


def test_request_bodies_are_bounded(served):  # noqa: F811
    client, _ = served
    assert client.post("/api/chaos", json={"command": ""}).status_code == 422
    assert client.post("/api/chaos", json={"command": "x" * 501}).status_code == 422
    assert client.post("/api/inject", json={"node_id": "../etc", "fault_class": "disk_full"}).status_code == 422
    assert client.post("/api/inject", json={"node_id": "r1-01", "fault_class": "x" * 65}).status_code == 422
    assert client.post("/api/storm", json={"rows": [0]}).status_code == 422
    assert client.post("/api/storm", json={"rows": [1, 2, 3, 4]}).status_code == 422
    assert client.post("/api/storm", json={"rows": 99}).status_code == 422


def test_promote_rejects_unknown_classes(served):  # noqa: F811
    client, orch = served
    orch.promoter = object()  # must not be reached
    assert client.post("/api/promote/run", params={"fault_class": "gremlins"}).status_code == 422


def test_history_since_is_clamped(served):  # noqa: F811
    client, _ = served
    for since in ("-5", "nan", "inf"):
        reply = client.get("/api/history", params={"since": since})
        assert reply.status_code == 200 and reply.json()["events"] == []
    assert server.clamp_since(float("nan")) == 0.0 and server.clamp_since(12.5) == 12.5


def _vercel(monkeypatch, path):
    monkeypatch.setenv("UNDERSTUDY_DB", str(path))
    from understudy import vercel_app

    return importlib.reload(vercel_app)


@pytest.mark.parametrize("content", [None, b"this is not a sqlite database" * 100, b""])
def test_vercel_app_never_500s_on_a_bad_snapshot(monkeypatch, tmp_path, content):
    db = tmp_path / "understudy.db"
    if content is not None:
        db.write_bytes(content)
    module = _vercel(monkeypatch, db)
    with TestClient(module.app, raise_server_exceptions=False) as client:
        health = client.get("/healthz").json()
        assert health["ok"] is True and health["db"] is (content is not None) and health["db_readable"] is False
        ledger = client.get("/api/ledger")
        assert ledger.status_code == 200 and ledger.json()["incidents_total"] == 0
        assert client.get("/api/ladder").json() == {"ladder": {}, "events": []}
        assert client.get("/api/skills").json() == []
        assert client.get("/api/history", params={"since": "nan"}).json() == {"events": []}
        assert client.get("/ws").status_code == 501
