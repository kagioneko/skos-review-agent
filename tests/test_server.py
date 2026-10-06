"""The HTTP API in offline-demo mode (no Gemini, no credentials)."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(monkeypatch_module):  # type: ignore[no-untyped-def]
    monkeypatch_module.setenv("REVIEW_AGENT_MODEL", "offline")
    import agent.agent
    import agent.runtime
    import web.server

    importlib.reload(agent.agent)
    importlib.reload(agent.runtime)
    server = importlib.reload(web.server)
    with TestClient(server.app) as c:
        yield c
    monkeypatch_module.delenv("REVIEW_AGENT_MODEL")
    importlib.reload(agent.agent)
    importlib.reload(agent.runtime)


@pytest.fixture(scope="module")
def monkeypatch_module():  # type: ignore[no-untyped-def]
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def test_demo_story_end_to_end(client: TestClient) -> None:
    assert client.get("/api/info").json()["mode"] == "offline-demo"
    sid = client.post("/api/session", json={"config": None}).json()["session_id"]
    res = client.post("/api/chat", json={"session_id": sid, "message": "review"}).json()
    kinds = [s["kind"] for s in res["steps"]]
    assert "hold" in kinds
    assert res["gate"]["outside_content_read"] and res["gate"]["private_data_read"]
    assert res["gate"]["holds"][-1]["reason"].startswith("CAPGRAPH-001")
    assert res["outbox"] == []
    hold = next(s for s in res["steps"] if s["kind"] == "hold")
    after = client.post("/api/confirm", json={
        "session_id": sid, "confirmation_id": hold["confirmation_id"], "confirmed": False,
    }).json()
    assert after["outbox"] == []


def test_security_headers_and_page(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert client.get("/docs").status_code == 404


@pytest.mark.parametrize("body", [
    {"session_id": "not-a-session", "message": "x"},
    {"session_id": "0" * 32, "message": ""},
    {"session_id": "0" * 32, "message": "x" * 4001},
])
def test_bad_chat_requests_are_rejected(client: TestClient, body: dict) -> None:
    assert client.post("/api/chat", json=body).status_code in (404, 422)


def test_oversized_config_is_rejected(client: TestClient) -> None:
    assert client.post("/api/session", json={"config": "x" * 200_001}).status_code == 422


def test_invalid_config_is_reported_without_values(client: TestClient) -> None:
    secret = "ghp_" + "Z" * 36
    sid = client.post("/api/session", json={"config": '{"nope": "' + secret + '"}'}).json()[
        "session_id"
    ]
    res = client.post("/api/chat", json={"session_id": sid, "message": "review"})
    assert secret not in res.text


def test_session_reports_scripted_mode(client: TestClient) -> None:
    res = client.post("/api/session", json={"config": None, "scripted": False}).json()
    assert res["scripted"] is True  # offline server: every session is scripted


def test_scripted_session_replays_the_injection_without_gemini() -> None:
    import asyncio

    from agent.runtime import Runtime
    from web.server import SAMPLE

    async def go() -> tuple[dict, dict]:
        rt = Runtime()
        sid = await rt.create(SAMPLE, scripted=True)
        res = await rt.chat(sid, "review")
        hold = next(s for s in res["steps"] if s["kind"] == "hold")
        return res, await rt.confirm(sid, hold["confirmation_id"], False)

    res, after = asyncio.run(go())
    assert res["gate"]["holds"][-1]["reason"].startswith("CAPGRAPH-001")
    assert res["outbox"] == [] and after["outbox"] == []
    assert any(s["kind"] == "text" and "台本モード" in s["text"] for s in after["steps"])
