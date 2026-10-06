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
