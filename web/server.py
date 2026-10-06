"""HTTP API + the demo page.

POST /api/session  {"config": "<MCP config JSON>" | null, "scripted": bool}
                   -> {"session_id", "scripted"}
                   (null = the bundled sample config; scripted = replay a model
                   that falls for the injection, no Gemini call)
POST /api/chat     {"session_id", "message"}               -> steps, gate, outbox
POST /api/confirm  {"session_id", "confirmation_id", "confirmed"}
GET  /api/info     -> mode (gemini model or offline demo)

Nothing the user sends is stored beyond the in-memory session or logged.

Public-demo protections, applied before any request body is parsed: a body
size cap (counted as bytes arrive, not trusted from Content-Length) and a
rate limit per client plus a global one. The client address comes from
X-Forwarded-For only as far as TRUSTED_PROXY_HOPS trusted proxies put it
there (Cloud Run: 1 - its front end appends the address it saw); anything
to the left of that is client-supplied and ignored. Validation errors never
echo the input back.
"""

from __future__ import annotations

import os
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from agent.agent import MODEL
from agent.runtime import NotPending, Runtime, Unavailable, offline_mode
from agent.skos_tools import MAX_CONFIG_CHARS, ensure_packs

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / "static"
SAMPLE = (ROOT / "demo" / "sample-mcp.json").read_text(encoding="utf-8")
RATE_WINDOW = 60.0
RATE_LIMIT = 20  # API calls per client per minute
GLOBAL_RATE_LIMIT = int(os.environ.get("REVIEW_AGENT_GLOBAL_RATE", "120"))  # all clients
MAX_CLIENTS = 10_000
MAX_BODY = 256 * 1024  # bytes; a 50k-character config fits with JSON escaping
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))

app = FastAPI(title="SKOS Review Agent", docs_url=None, redoc_url=None, openapi_url=None)
runtime = Runtime()
_calls: dict[str, deque[float]] = {}
_all_calls: deque[float] = deque()


@app.on_event("startup")
def _startup() -> None:
    ensure_packs()  # verify the signed packs before serving


def client_ip(headers: dict[str, str], peer: str | None) -> str:
    """The address the nearest trusted proxy saw. Each trusted proxy appends
    to X-Forwarded-For, so with N hops the N-th entry from the right is the
    one written by the outermost trusted proxy; entries further left are
    whatever the client sent."""
    if TRUSTED_PROXY_HOPS > 0:
        parts = [p.strip() for p in headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(parts) >= TRUSTED_PROXY_HOPS:
            return parts[-TRUSTED_PROXY_HOPS]
    return peer or "?"


def _prune(q: deque[float], now: float) -> None:
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()


def rate_check(client: str, now: float | None = None) -> str | None:
    """None if the call may go ahead, else the reason it may not."""
    now = time.time() if now is None else now
    _prune(_all_calls, now)
    if len(_all_calls) >= GLOBAL_RATE_LIMIT:
        return "the demo is busy, try again in a minute"
    q = _calls.get(client)
    if q is None:
        if len(_calls) >= MAX_CLIENTS:
            for key in [k for k, v in _calls.items() if not v or now - v[-1] > RATE_WINDOW]:
                del _calls[key]
            if len(_calls) >= MAX_CLIENTS:
                return "the demo is busy, try again in a minute"
        q = _calls[client] = deque()
    _prune(q, now)
    if len(q) >= RATE_LIMIT:
        return "too many requests, try again in a minute"
    q.append(now)
    _all_calls.append(now)
    return None


class Guard:
    """ASGI middleware: rate limit and body cap for /api/ POSTs, before the
    body is read or parsed (so 422s and oversized bodies count too)."""

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or not (
            scope["method"] == "POST" and scope["path"].startswith("/api/")
        ):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        peer = scope.get("client")[0] if scope.get("client") else None
        reason = rate_check(client_ip(headers, peer))
        if reason:
            return await JSONResponse({"detail": reason}, status_code=429)(scope, receive, send)
        try:
            declared = int(headers.get("content-length", "0"))
        except ValueError:
            declared = MAX_BODY + 1
        if declared > MAX_BODY:
            return await JSONResponse({"detail": "request too large"}, status_code=413)(
                scope, receive, send
            )
        seen = 0

        async def limited():  # type: ignore[no-untyped-def]
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > MAX_BODY:
                    raise _TooLarge
            return message

        try:
            return await self.app(scope, limited, send)
        except _TooLarge:
            return await JSONResponse({"detail": "request too large"}, status_code=413)(
                scope, receive, send
            )


class _TooLarge(Exception):
    pass


@app.exception_handler(RequestValidationError)
async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
    # never echo the input (it may be a config with secrets): location and type only
    errors = [{"loc": list(e.get("loc", ())), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


class NewSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: str | None = Field(default=None, max_length=MAX_CONFIG_CHARS)
    scripted: bool = False


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=4000)


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmation_id: str = Field(min_length=1, max_length=200)
    confirmed: bool


@app.get("/api/info")
def info() -> dict:
    return {"mode": "offline-demo" if offline_mode() else "gemini", "model": MODEL}


@app.post("/api/session")
async def new_session(body: NewSession) -> dict:
    scripted = body.scripted or offline_mode()
    try:
        sid = await runtime.create(body.config or SAMPLE, scripted=scripted)
    except Unavailable as exc:
        raise HTTPException(503, str(exc)) from None
    return {"session_id": sid, "scripted": scripted}


async def _call(coro):  # type: ignore[no-untyped-def]
    try:
        return await coro
    except KeyError:
        raise HTTPException(404, "unknown or expired session") from None
    except NotPending as exc:
        raise HTTPException(409, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(429, str(exc)) from None
    except Unavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except TimeoutError:
        raise HTTPException(504, "the agent took too long; try again") from None


@app.post("/api/chat")
async def chat(body: Chat) -> dict:
    return await _call(runtime.chat(body.session_id, body.message))


@app.post("/api/confirm")
async def confirm(body: Confirm) -> dict:
    return await _call(runtime.confirm(body.session_id, body.confirmation_id, body.confirmed))


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.add_middleware(Guard)


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
