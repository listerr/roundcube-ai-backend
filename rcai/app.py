# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""The ASGI application.

Routes:
  GET|POST <base>/?fn=...           data API
  POST     <base>/v1/chat/completions  chat
  GET      <base>/health             liveness check, no authentication

<base> is [server] base_path, e.g. /rcapi. Each route also works without
the prefix, so the reverse proxy can pass the path as it is or strip the
prefix.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from . import __version__, chat, data
from .auth import LoginLimiter
from .config import Config
from .db import Database
from .mail import ImapBackend


class StripPrefix:
    """ASGI middleware: /rcapi/x -> /x. Paths without the prefix pass unchanged."""

    def __init__(self, app, prefix: str):
        self.app, self.prefix = app, prefix.rstrip("/")

    async def __call__(self, scope, receive, send):
        if self.prefix and scope["type"] in ("http", "websocket"):
            path = scope["path"]
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix):] or "/"
                raw = scope.get("raw_path")
                if raw is not None and raw.startswith(self.prefix.encode()):
                    scope["raw_path"] = raw[len(self.prefix):] or b"/"
        await self.app(scope, receive, send)


class BodyLimit:
    """ASGI middleware: refuses request bodies over max_bytes with 413.

    It reads the body before the app does, so the app never gets more than
    max_bytes, also without nginx in front.
    """

    def __init__(self, app, max_bytes: int):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max):
            return await self._refuse(send)
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > self.max:
                return await self._refuse(send)
            chunks.append(chunk)
            if not msg.get("more_body"):
                break
        body, sent = b"".join(chunks), False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _refuse(self, send):
        payload = json.dumps({"error": "The request is too large.", "code": 413}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())]})
        await send({"type": "http.response.body", "body": payload})


def create_api(cfg: Config, *, mail=None, llm=None, db: Database | None = None) -> FastAPI:
    """The FastAPI app. mail, llm and db can be replaced (tests)."""
    api = FastAPI(title="roundcube-ai-backend", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    st = api.state
    st.cfg = cfg
    st.db = db or Database(cfg.database.path)
    st.db.recover()
    st.mail = mail or ImapBackend(cfg.imap)
    st.llm = llm or AsyncOpenAI(api_key=cfg.openai.api_key, base_url=cfg.openai.base_url,
                                timeout=cfg.openai.timeout, max_retries=2)
    st.limiter = LoginLimiter(cfg.auth.max_failures, cfg.auth.max_failures_ip, cfg.auth.failure_window)
    st.logins_active = 0
    st.active = 0
    st.active_by_box = {}           # mailbox id -> agent runs
    st.quota_cache = {}             # login -> (time, quota tuple)
    st.jobs = set()

    api.add_middleware(BodyLimit, max_bytes=cfg.server.max_body_bytes)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Conversation-Id", "X-Chat-Type"],
        expose_headers=["X-Conversation-Id", "Retry-After"],
        max_age=600,
    )

    @api.exception_handler(data.ApiError)
    async def _api_error(_request: Request, e: data.ApiError):
        return JSONResponse({"error": e.message, "code": e.code}, status_code=e.code)

    @api.get("/health")
    async def health():
        return {"ok": True, "version": __version__, "active": st.active}

    api.include_router(data.router)
    api.include_router(chat.router)
    return api


def create_app(cfg: Config, **kw) -> StripPrefix:
    return StripPrefix(create_api(cfg, **kw), cfg.server.base_path)
