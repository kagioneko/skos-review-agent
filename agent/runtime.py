"""Sessions and a UI-friendly view of what the agent did.

One ADK runner per process, sessions in memory only: the user's MCP config
lives in the session state and is never written to disk or logged. Sessions
expire and are capped in number.
"""

from __future__ import annotations

import os
import time
import uuid
from collections import OrderedDict
from typing import Any

from google.adk.agents import LlmAgent
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


def offline_mode() -> bool:
    return MODEL == "offline"


class Runtime:
    def __init__(self) -> None:
        self._runners: dict[str, InMemoryRunner] = {}
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _runner(self, sid: str) -> InMemoryRunner:
        # offline: a fresh scripted model per session so each demo replays
        if offline_mode():
            from agent.offline import demo_model

            model: Any = demo_model()
        else:
            model = MODEL
        agent = LlmAgent(
            name="skos_review_agent", model=model, instruction=INSTRUCTION,
            tools=TOOLS, before_tool_callback=before_tool,
        )
        return InMemoryRunner(agent=agent, app_name=APP)

    def _expire(self) -> None:
        now = time.time()
        for sid in [s for s, meta in self._sessions.items() if now - meta["at"] > SESSION_TTL]:
            self._drop(sid)
        while len(self._sessions) > MAX_SESSIONS:
            self._drop(next(iter(self._sessions)))

    def _drop(self, sid: str) -> None:
        self._sessions.pop(sid, None)
        self._runners.pop(sid, None)

    async def create(self, config_text: str) -> str:
        self._expire()
        sid = uuid.uuid4().hex
        runner = self._runner(sid)
        await runner.session_service.create_session(
            app_name=APP, user_id=sid, session_id=sid,
            state={"user:mcp_config": config_text},
        )
        self._runners[sid] = runner
        self._sessions[sid] = {"at": time.time(), "turns": 0}
        return sid

    def _get(self, sid: str) -> InMemoryRunner:
        self._expire()
        if sid not in self._runners:
            raise KeyError("unknown or expired session")
        meta = self._sessions[sid]
        if meta["turns"] >= MAX_TURNS_PER_SESSION:
            raise PermissionError("turn limit for this session reached")
        meta["turns"] += 1
        meta["at"] = time.time()
        self._sessions.move_to_end(sid)
        return self._runners[sid]

    async def _run(self, sid: str, message: types.Content) -> dict[str, Any]:
        runner = self._get(sid)
        steps: list[dict[str, Any]] = []
        async for ev in runner.run_async(user_id=sid, session_id=sid, new_message=message):
            steps.extend(_view(ev))
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

    async def chat(self, sid: str, text: str) -> dict[str, Any]:
        text = text[:MAX_MESSAGE_CHARS]
        return await self._run(sid, types.Content(role="user", parts=[types.Part(text=text)]))

    async def confirm(self, sid: str, confirmation_id: str, confirmed: bool) -> dict[str, Any]:
        part = types.Part(function_response=types.FunctionResponse(
            id=confirmation_id, name=CONFIRMATION, response={"confirmed": bool(confirmed)},
        ))
        return await self._run(sid, types.Content(role="user", parts=[part]))


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
