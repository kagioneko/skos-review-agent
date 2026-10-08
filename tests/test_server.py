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


@pytest.fixture(autouse=True)
def _fresh_rate_limits():  # type: ignore[no-untyped-def]
    import web.server as ws

    ws._calls.clear()
    ws._all_calls.clear()
    ws._creates.clear()
    yield


def _held(client: TestClient) -> tuple[str, str]:
    sid = client.post("/api/session", json={"config": None}).json()["session_id"]
    res = client.post("/api/chat", json={"session_id": sid, "message": "review"}).json()
    return sid, next(s for s in res["steps"] if s["kind"] == "hold")["confirmation_id"]


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
    assert client.post("/api/session", json={"config": "x" * 50_001}).status_code == 422


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


def test_answered_approval_cannot_be_replayed(client: TestClient) -> None:
    # regression: ADK alone accepted a second answer for the same id, so a
    # rejected send could be re-sent as approved, and an approved one twice
    sid, hid = _held(client)
    no = client.post("/api/confirm", json={"session_id": sid, "confirmation_id": hid,
                                           "confirmed": False})
    assert no.status_code == 200 and no.json()["outbox"] == []
    again = client.post("/api/confirm", json={"session_id": sid, "confirmation_id": hid,
                                              "confirmed": True})
    assert again.status_code == 409

    sid, hid = _held(client)
    yes = client.post("/api/confirm", json={"session_id": sid, "confirmation_id": hid,
                                            "confirmed": True}).json()
    assert len(yes["outbox"]) == 1
    assert any("許可したので" in s.get("text", "") for s in yes["steps"])
    twice = client.post("/api/confirm", json={"session_id": sid, "confirmation_id": hid,
                                              "confirmed": True})
    assert twice.status_code == 409


def test_confirm_needs_an_id_this_session_issued(client: TestClient) -> None:
    sid_a, hid_a = _held(client)
    sid_b = client.post("/api/session", json={"config": None}).json()["session_id"]
    for sid, cid in [(sid_a, "adk-made-up"), (sid_b, hid_a)]:
        r = client.post("/api/confirm", json={"session_id": sid, "confirmation_id": cid,
                                              "confirmed": True})
        assert r.status_code == 409


def test_validation_errors_do_not_echo_input(client: TestClient) -> None:
    canary = "SECRET_CANARY_123"
    for body in [{"config": canary + "x" * 50_001}, {"config": {"k": canary}},
                 {"config": None, "extra": canary}, {canary: None}]:
        r = client.post("/api/session", json=body)
        assert r.status_code == 422
        assert canary not in r.text


def test_oversized_body_is_refused_before_parsing(client: TestClient) -> None:
    r = client.post("/api/session", content=b'{"config": "' + b"x" * 300_000 + b'"}',
                    headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_rate_limit_counts_requests_that_fail_validation(client: TestClient) -> None:
    codes = [client.post("/api/chat", json={}).status_code for _ in range(25)]
    assert codes[:20] == [422] * 20 and 429 in codes[20:]


def test_streamed_oversized_body_is_refused_before_the_route(client: TestClient) -> None:
    def chunks():  # type: ignore[no-untyped-def]
        yield b'{"config": "'
        for _ in range(40):
            yield b"x" * 10_000
        yield b'"}'

    r = client.post("/api/session", content=chunks(), headers={"content-type": "application/json"})
    assert "content-length" not in r.request.headers
    assert r.status_code == 413
