"""Sessions and a UI-friendly view of what the agent did.

One ADK runner per session, sessions in memory only: the user's MCP config
lives in the session state and is never logged (SKOS reads it from a
private temp file that is deleted right after the scan). Sessions expire and
are capped in number; when full, new sessions are refused rather than
evicting someone else's.

Public-demo limits on top of that: a confirmation id can be answered once
and only if this session issued it; one request per session at a time; and
live (Gemini) turns share a daily budget, a concurrency cap, a per-run
model-call cap and a timeout.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import OrderedDict
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.agent import INSTRUCTION, MODEL
from agent.gate import HOLDS, SENSITIVE, UNTRUSTED, before_tool
from agent.tools import OUTBOX, TOOLS

APP = "skos-review-agent"
MAX_SESSIONS = int(os.environ.get("REVIEW_AGENT_MAX_SESSIONS", "200"))
SESSION_TTL = int(os.environ.get("REVIEW_AGENT_SESSION_TTL", "3600"))
MAX_MESSAGE_CHARS = 4000
MAX_TURNS_PER_SESSION = int(os.environ.get("REVIEW_AGENT_MAX_TURNS", "30"))
CONFIRMATION = "adk_request_confirmation"
LIVE_DAILY_TURNS = int(os.environ.get("REVIEW_AGENT_LIVE_DAILY_TURNS", "300"))
LIVE_CONCURRENCY = int(os.environ.get("REVIEW_AGENT_LIVE_CONCURRENCY", "2"))
MAX_LLM_CALLS = int(os.environ.get("REVIEW_AGENT_MAX_LLM_CALLS", "12"))
MAX_OUTPUT_TOKENS = int(os.environ.get("REVIEW_AGENT_MAX_OUTPUT_TOKENS", "2048"))
RUN_TIMEOUT = float(os.environ.get("REVIEW_AGENT_RUN_TIMEOUT", "120"))


class Unavailable(Exception):
    """The demo is at a capacity or budget limit; try later (HTTP 503)."""


class NotPending(Exception):
    """The confirmation id was not issued by this session or is already answered."""


def offline_mode() -> bool:
    return MODEL == "offline"


class LiveBudget:
    """Daily cap on live (Gemini) turns for the whole process, plus a
    concurrency cap. With --max-instances 1 this bounds the demo's spend."""

    def __init__(self, daily: int, concurrency: int) -> None:
        self.daily = daily
        self._day = ""
        self._used = 0
        self._sem = asyncio.Semaphore(concurrency)

    def take(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._day:
            self._day, self._used = today, 0
        if self._used >= self.daily:
            raise Unavailable("today's live Gemini budget is used up; try scripted mode")
        self._used += 1

    @property
    def sem(self) -> asyncio.Semaphore:
        return self._sem


class Runtime:
    def __init__(self) -> None:
        self._runners: dict[str, InMemoryRunner] = {}
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.budget = LiveBudget(LIVE_DAILY_TURNS, LIVE_CONCURRENCY)

    def _runner(self, scripted: bool) -> InMemoryRunner:
        # scripted (or offline): a fresh scripted model per session so each
        # demo replays - it plays a model that falls for the injection
        if scripted or offline_mode():
            from agent.offline import demo_model

            model: Any = demo_model()
        else:
            model = MODEL
        agent = LlmAgent(
            name="skos_review_agent", model=model, instruction=INSTRUCTION,
            tools=TOOLS, before_tool_callback=before_tool,
            generate_content_config=types.GenerateContentConfig(
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
        return InMemoryRunner(agent=agent, app_name=APP)

    def _expire(self) -> None:
        now = time.time()
        for sid in [s for s, meta in self._sessions.items() if now - meta["at"] > SESSION_TTL]:
            self._drop(sid)

    def _drop(self, sid: str) -> None:
        self._sessions.pop(sid, None)
        self._runners.pop(sid, None)

    async def create(self, config_text: str, scripted: bool = False) -> str:
        self._expire()
        if len(self._sessions) >= MAX_SESSIONS:
            raise Unavailable("the demo is busy; try again later")
        sid = uuid.uuid4().hex
        scripted = scripted or offline_mode()
        runner = self._runner(scripted)
        await runner.session_service.create_session(
            app_name=APP, user_id=sid, session_id=sid,
            state={"user:mcp_config": config_text},
        )
        self._runners[sid] = runner
        self._sessions[sid] = {
            "at": time.time(), "turns": 0, "scripted": scripted,
            "pending": set(), "lock": asyncio.Lock(),
        }
        return sid

    def _meta(self, sid: str) -> dict[str, Any]:
        self._expire()
        if sid not in self._runners:
            raise KeyError("unknown or expired session")
        return self._sessions[sid]

    def _start_turn(self, sid: str, meta: dict[str, Any]) -> InMemoryRunner:
        if meta["turns"] >= MAX_TURNS_PER_SESSION:
            raise PermissionError("turn limit for this session reached")
        if not meta["scripted"]:
            self.budget.take()
        meta["turns"] += 1
        meta["at"] = time.time()
        self._sessions.move_to_end(sid)
        return self._runners[sid]

    async def _run(
        self, sid: str, message: types.Content, confirmation_id: str | None = None
    ) -> dict[str, Any]:
        meta = self._meta(sid)
        if meta["lock"].locked():
            raise PermissionError("this session is already working on a request")
        async with meta["lock"]:
            if confirmation_id is not None and confirmation_id not in meta["pending"]:
                raise NotPending("no such pending approval in this session")
            runner = self._start_turn(sid, meta)
            if confirmation_id is not None:
                meta["pending"].discard(confirmation_id)  # answered once, never again
            steps = await self._collect(runner, sid, message, live=not meta["scripted"])
            meta["pending"].update(
                s["confirmation_id"] for s in steps if s["kind"] == "hold"
            )
        session = await runner.session_service.get_session(
            app_name=APP, user_id=sid, session_id=sid
        )
        state = session.state if session else {}
        return {
            "steps": steps,
            "gate": {
                "outside_content_read": bool(state.get(UNTRUSTED)),
                "private_data_read": bool(state.get(SENSITIVE)),
                "holds": list(state.get(HOLDS) or []),
            },
            "outbox": list(state.get(OUTBOX) or []),
        }

    async def _collect(
        self, runner: InMemoryRunner, sid: str, message: types.Content, live: bool
    ) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []

        async def go() -> None:
            async for ev in runner.run_async(
                user_id=sid, session_id=sid, new_message=message,
                run_config=RunConfig(max_llm_calls=MAX_LLM_CALLS),
            ):
                steps.extend(_view(ev))

        if not live:
            await asyncio.wait_for(go(), RUN_TIMEOUT)
            return steps
        async with self.budget.sem:
            await asyncio.wait_for(go(), RUN_TIMEOUT)
        return steps

    async def chat(self, sid: str, text: str) -> dict[str, Any]:
        text = text[:MAX_MESSAGE_CHARS]
        return await self._run(sid, types.Content(role="user", parts=[types.Part(text=text)]))

    async def confirm(self, sid: str, confirmation_id: str, confirmed: bool) -> dict[str, Any]:
        part = types.Part(function_response=types.FunctionResponse(
            id=confirmation_id, name=CONFIRMATION, response={"confirmed": bool(confirmed)},
        ))
        return await self._run(
            sid, types.Content(role="user", parts=[part]), confirmation_id=confirmation_id
        )


def _view(ev: Any) -> list[dict[str, Any]]:
    """Plain data for the UI. Everything is rendered as text there."""
    out: list[dict[str, Any]] = []
    for p in (ev.content.parts if ev.content else None) or []:
        if p.function_call:
            fc = p.function_call
            if fc.name == CONFIRMATION:
                original = (fc.args or {}).get("originalFunctionCall") or {}
                out.append({
                    "kind": "hold", "confirmation_id": fc.id,
                    "tool": original.get("name"), "args": original.get("args") or {},
                })
            else:
                out.append({"kind": "call", "tool": fc.name, "args": dict(fc.args or {})})
        elif p.function_response:
            fr = p.function_response
            if fr.name != CONFIRMATION:
                out.append({"kind": "result", "tool": fr.name, "result": fr.response})
        elif p.text:
            out.append({"kind": "text", "author": ev.author, "text": p.text})
    return out
