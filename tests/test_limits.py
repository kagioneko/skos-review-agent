"""Public-demo limits that need no HTTP client."""

from __future__ import annotations

import asyncio
import json

import pytest


def test_client_ip_trusts_only_the_proxy_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    import web.server as ws

    spoofed = {"x-forwarded-for": "6.6.6.6, 203.0.113.9"}
    monkeypatch.setattr(ws, "TRUSTED_PROXY_HOPS", 0)
    assert ws.client_ip(spoofed, "10.0.0.1") == "10.0.0.1"
    monkeypatch.setattr(ws, "TRUSTED_PROXY_HOPS", 1)
    assert ws.client_ip(spoofed, "10.0.0.1") == "203.0.113.9"
    assert ws.client_ip({}, "10.0.0.1") == "10.0.0.1"


def test_rate_limits_per_client_and_global(monkeypatch: pytest.MonkeyPatch) -> None:
    import web.server as ws

    monkeypatch.setattr(ws, "_calls", {})
    monkeypatch.setattr(ws, "_all_calls", ws.deque())
    monkeypatch.setattr(ws, "GLOBAL_RATE_LIMIT", 30)
    assert all(ws.rate_check("a", 100.0) is None for _ in range(20))
    assert ws.rate_check("a", 100.0) is not None
    assert all(ws.rate_check(f"c{i}", 100.0) is None for i in range(10))
    assert ws.rate_check("new", 100.0) == "the demo is busy, try again in a minute"
    assert ws.rate_check("a", 200.0) is None  # window passed


def test_client_table_is_pruned_not_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    import web.server as ws

    monkeypatch.setattr(ws, "_calls", {})
    monkeypatch.setattr(ws, "_all_calls", ws.deque())
    monkeypatch.setattr(ws, "MAX_CLIENTS", 3)
    monkeypatch.setattr(ws, "GLOBAL_RATE_LIMIT", 10_000)
    for c in "abc":
        assert ws.rate_check(c, 100.0) is None
    for _ in range(19):
        ws.rate_check("a", 100.0)
    assert ws.rate_check("d", 110.0) is not None  # full of recent clients: refused
    assert ws.rate_check("a", 110.0) is not None  # and "a" keeps its history
    assert ws.rate_check("d", 200.0) is None      # stale entries pruned


def test_too_many_servers_are_refused() -> None:
    from agent import skos_tools

    servers = {f"s{i}": {"command": "npx", "args": ["x"]} for i in range(skos_tools.MAX_SERVERS + 1)}
    out = skos_tools.scan(json.dumps({"mcpServers": servers}), "none")
    assert out == {"error": f"too many servers: this demo reviews up to {skos_tools.MAX_SERVERS}"}


def test_full_runtime_refuses_instead_of_evicting(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.runtime as rt

    monkeypatch.setattr(rt, "MAX_SESSIONS", 2)

    async def go() -> None:
        r = rt.Runtime()
        first = await r.create("{}", scripted=True)
        await r.create("{}", scripted=True)
        with pytest.raises(rt.Unavailable):
            await r.create("{}", scripted=True)
        assert first in r._sessions

    asyncio.run(go())


def test_live_budget_is_enforced_before_calling_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.runtime as rt

    monkeypatch.setattr(rt, "MODEL", "gemini-2.5-flash")

    async def go() -> None:
        r = rt.Runtime()
        r.budget = rt.LiveBudget(daily=0, concurrency=1)
        sid = await r.create("{}", scripted=False)
        with pytest.raises(rt.Unavailable):
            await r.chat(sid, "review")  # refused before any model call
        scripted = await r.create("{}", scripted=True)
        await r.chat(scripted, "review")  # scripted sessions do not use the budget

    asyncio.run(go())


def test_daily_budget_is_shared_through_the_counter() -> None:
    # a restarted process builds a new LiveBudget; the shared counter keeps the count
    import agent.runtime as rt

    shared = rt.MemoryCounter()

    async def go() -> None:
        await rt.LiveBudget(daily=1, concurrency=1, counter=shared).take()
        with pytest.raises(rt.Unavailable):
            await rt.LiveBudget(daily=1, concurrency=1, counter=shared).take()

    asyncio.run(go())


def test_budget_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.runtime as rt

    async def go() -> None:
        with pytest.raises(rt.Unavailable):
            await rt.LiveBudget(daily=10, concurrency=1, counter=rt.BrokenCounter()).take()

    asyncio.run(go())

    def boom() -> None:
        raise RuntimeError("no credentials")

    monkeypatch.delenv("REVIEW_AGENT_BUDGET_STORE", raising=False)
    monkeypatch.setenv("K_SERVICE", "skos-review-agent")  # on Cloud Run
    monkeypatch.setattr(rt, "FirestoreCounter", boom)
    assert isinstance(rt.budget_counter(), rt.BrokenCounter)
    monkeypatch.delenv("K_SERVICE")
    assert isinstance(rt.budget_counter(), rt.MemoryCounter)  # local run


def _live_session_with_hold(r):  # type: ignore[no-untyped-def]
    """A session that holds a send, then treated as live (no Gemini needed)."""
    from web.server import SAMPLE

    async def go():  # type: ignore[no-untyped-def]
        sid = await r.create(SAMPLE, scripted=True)
        res = await r.chat(sid, "review")
        r._sessions[sid]["scripted"] = False
        return sid, next(s for s in res["steps"] if s["kind"] == "hold")["confirmation_id"]

    return go()


def test_busy_live_slot_refuses_without_spending_anything() -> None:
    import agent.runtime as rt

    async def go() -> None:
        r = rt.Runtime()
        counter = rt.MemoryCounter()
        r.budget = rt.LiveBudget(daily=5, concurrency=1, counter=counter)
        sid, hid = await _live_session_with_hold(r)
        turns = r._sessions[sid]["turns"]
        async with r.budget.sem:  # someone else holds the only slot
            with pytest.raises(rt.Unavailable):
                await asyncio.wait_for(r.confirm(sid, hid, True), 1)  # refused, not queued
            with pytest.raises(rt.Unavailable):
                await asyncio.wait_for(r.chat(sid, "hi"), 1)
        meta = r._sessions[sid]
        assert hid in meta["pending"] and meta["turns"] == turns and counter._used == 0
        await r.confirm(sid, hid, False)  # the slot is free again: the answer still counts
        assert hid not in r._sessions[sid]["pending"] and counter._used == 1

    asyncio.run(go())


def test_failed_run_ends_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.runtime as rt
    from web.server import SAMPLE

    monkeypatch.setattr(rt, "RUN_TIMEOUT", 0.0)

    async def go() -> None:
        r = rt.Runtime()
        sid = await r.create(SAMPLE, scripted=True)
        with pytest.raises(TimeoutError):
            await r.chat(sid, "review")
        with pytest.raises(KeyError):
            await r.chat(sid, "again")

    asyncio.run(go())


# --- Codex round 3 nits -------------------------------------------------------


def test_memory_or_unknown_budget_store_is_refused_on_cloud_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # N1: on Cloud Run a per-process counter would reset with every instance
    import agent.runtime as rt

    monkeypatch.setenv("K_SERVICE", "skos-review-agent")
    monkeypatch.setenv("REVIEW_AGENT_BUDGET_STORE", "memory")
    assert isinstance(rt.budget_counter(), rt.BrokenCounter)
    monkeypatch.setenv("REVIEW_AGENT_BUDGET_STORE", "redis")
    assert isinstance(rt.budget_counter(), rt.BrokenCounter)
    monkeypatch.delenv("K_SERVICE")
    assert isinstance(rt.budget_counter(), rt.BrokenCounter)  # unknown name, locally too
    monkeypatch.setenv("REVIEW_AGENT_BUDGET_STORE", "memory")
    assert isinstance(rt.budget_counter(), rt.MemoryCounter)


def test_slow_budget_store_is_refused_not_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # N3: LiveBudget stops waiting; the store's own RPC timeout bounds the thread
    import time as _time

    import agent.runtime as rt

    class Slow:
        def take(self, day: str, daily: int) -> bool:
            _time.sleep(0.5)
            return True

    monkeypatch.setattr(rt, "BUDGET_TIMEOUT", 0.05)

    async def go() -> None:
        with pytest.raises(rt.Unavailable):
            await rt.LiveBudget(daily=10, concurrency=1, counter=Slow()).take()

    asyncio.run(go())


def _run_guard(messages, timeout: float = 1.0):  # type: ignore[no-untyped-def]
    """Drive the Guard middleware with a hand-written receive(); returns
    (status or None, body the app saw or None)."""
    import web.server as ws

    seen: dict = {}

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        seen["body"] = (await receive())["body"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    sent: list = []

    async def send(message):  # type: ignore[no-untyped-def]
        sent.append(message)

    queue = list(messages)

    async def receive():  # type: ignore[no-untyped-def]
        if queue:
            return queue.pop(0)
        await asyncio.sleep(3600)  # a client that never sends the rest

    scope = {"type": "http", "method": "POST", "path": "/api/chat", "headers": [],
             "client": ("192.0.2.1", 1)}

    async def go() -> None:
        await asyncio.wait_for(ws.Guard(app)(scope, receive, send), timeout)

    asyncio.run(go())
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, seen.get("body")


@pytest.fixture
def _guard_state(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    import web.server as ws

    monkeypatch.setattr(ws, "_calls", {})
    monkeypatch.setattr(ws, "_all_calls", ws.deque())
    monkeypatch.setattr(ws, "_creates", {})
    return ws


def test_guard_reassembles_a_chunked_body(_guard_state) -> None:  # type: ignore[no-untyped-def]
    msgs = [{"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False}]
    assert _run_guard(msgs) == (200, b"abcd")


def test_guard_refuses_oversize_chunks_before_the_app(  # type: ignore[no-untyped-def]
    _guard_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_guard_state, "MAX_BODY", 10)
    msgs = [{"type": "http.request", "body": b"x" * 6, "more_body": True},
            {"type": "http.request", "body": b"x" * 6, "more_body": True}]
    assert _run_guard(msgs) == (413, None)


def test_guard_times_out_a_trickled_body(  # type: ignore[no-untyped-def]
    _guard_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    # N2: a body that never finishes is answered 408 within BODY_TIMEOUT
    monkeypatch.setattr(_guard_state, "BODY_TIMEOUT", 0.05)
    msgs = [{"type": "http.request", "body": b"{", "more_body": True}]
    assert _run_guard(msgs) == (408, None)


def test_guard_stops_on_disconnect(_guard_state) -> None:  # type: ignore[no-untyped-def]
    msgs = [{"type": "http.request", "body": b"{", "more_body": True},
            {"type": "http.disconnect"}]
    assert _run_guard(msgs) == (None, None)


def test_session_creation_is_limited_per_client(_guard_state) -> None:  # type: ignore[no-untyped-def]
    ws = _guard_state
    assert all(ws.create_check("a", 100.0) is None for _ in range(ws.CREATE_LIMIT))
    assert ws.create_check("a", 100.0) is not None
    assert ws.create_check("b", 100.0) is None  # other clients unaffected
    assert ws.create_check("a", 100.0 + ws.CREATE_WINDOW + 1) is None


def test_unused_sessions_expire_quickly(monkeypatch: pytest.MonkeyPatch) -> None:
    # N4: an untouched session holds a table slot for minutes, not an hour
    import agent.runtime as rt
    from web.server import SAMPLE

    async def go() -> None:
        r = rt.Runtime()
        idle = await r.create("{}", scripted=True)
        used = await r.create(SAMPLE, scripted=True)
        await r.chat(used, "review")
        for sid in (idle, used):
            r._sessions[sid]["at"] -= rt.UNUSED_SESSION_TTL + 1
        r._expire()
        assert idle not in r._sessions and used in r._sessions

    asyncio.run(go())


def test_live_turns_are_capped_per_client_without_spending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # N4: one client cannot use the whole daily budget
    import agent.runtime as rt

    monkeypatch.setattr(rt, "LIVE_PER_CLIENT", 1)

    async def go() -> None:
        r = rt.Runtime()
        counter = rt.MemoryCounter()
        r.budget = rt.LiveBudget(daily=10, concurrency=1, counter=counter)
        sid, hid = await _live_session_with_hold(r)
        r._sessions[sid]["client"] = "203.0.113.9"
        await r.confirm(sid, hid, False)  # first live turn: allowed
        assert counter._used == 1
        with pytest.raises(rt.Unavailable):
            await r.chat(sid, "again")  # over this client's share
        assert counter._used == 1 and r._sessions[sid]["turns"] == 2
        other, _ = await _live_session_with_hold(r)
        r._sessions[other]["client"] = "198.51.100.7"
        await r.chat(other, "hi")  # another client still has its share
        assert counter._used == 2

    asyncio.run(go())


def test_reply_is_read_while_the_session_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    # N5: the state snapshot belongs to this turn only
    import agent.runtime as rt
    from web.server import SAMPLE

    async def go() -> None:
        r = rt.Runtime()
        sid = await r.create(SAMPLE, scripted=True)
        service = r._runners[sid].session_service
        real = service.get_session
        locked: list[bool] = []

        async def spy(**kw):  # type: ignore[no-untyped-def]
            locked.append(r._sessions[sid]["lock"].locked())
            return await real(**kw)

        monkeypatch.setattr(service, "get_session", spy)
        await r.chat(sid, "review")
        assert locked and all(locked)

    asyncio.run(go())
