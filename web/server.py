"""HTTP API + the demo page.

POST /api/session  {"config": "<MCP config JSON>" | null}  -> {"session_id"}
                   (null = the bundled sample config)
POST /api/chat     {"session_id", "message"}               -> steps, gate, outbox
POST /api/confirm  {"session_id", "confirmation_id", "confirmed"}
GET  /api/info     -> mode (gemini model or offline demo)

Nothing the user sends is stored beyond the in-memory session or logged.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.agent import MODEL
from agent.runtime import Runtime, offline_mode
from agent.skos_tools import MAX_CONFIG_CHARS, ensure_packs

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
SAMPLE = (ROOT / "demo" / "sample-mcp.json").read_text(encoding="utf-8")
RATE_WINDOW = 60.0
RATE_LIMIT = 20  # API calls per client per minute

app = FastAPI(title="SKOS Review Agent", docs_url=None, redoc_url=None, openapi_url=None)
runtime = Runtime()
_calls: dict[str, deque[float]] = defaultdict(deque)


@app.on_event("startup")
def _startup() -> None:
    ensure_packs()  # verify the signed packs before serving


def _limit(request: Request) -> None:
    client = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "?"
    )
    now = time.time()
    q = _calls[client]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "too many requests, try again in a minute")
    q.append(now)
    if len(_calls) > 10_000:
        _calls.clear()


class NewSession(BaseModel):
    config: str | None = Field(default=None, max_length=MAX_CONFIG_CHARS)


class Chat(BaseModel):
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=4000)


class Confirm(BaseModel):
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmation_id: str = Field(min_length=1, max_length=200)
    confirmed: bool


@app.get("/api/info")
def info() -> dict:
    return {"mode": "offline-demo" if offline_mode() else "gemini", "model": MODEL}


@app.post("/api/session")
async def new_session(body: NewSession, request: Request) -> dict:
    _limit(request)
    return {"session_id": await runtime.create(body.config or SAMPLE)}


@app.post("/api/chat")
async def chat(body: Chat, request: Request) -> dict:
    _limit(request)
    try:
        return await runtime.chat(body.session_id, body.message)
    except KeyError:
        raise HTTPException(404, "unknown or expired session") from None
    except PermissionError as exc:
        raise HTTPException(429, str(exc)) from None


@app.post("/api/confirm")
async def confirm(body: Confirm, request: Request) -> dict:
    _limit(request)
    try:
        return await runtime.confirm(body.session_id, body.confirmation_id, body.confirmed)
    except KeyError:
        raise HTTPException(404, "unknown or expired session") from None
    except PermissionError as exc:
        raise HTTPException(429, str(exc)) from None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
