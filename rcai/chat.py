# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""POST /v1/chat/completions: OpenAI chat with a server-side session.

Headers: Authorization (the token from do_login), X-Conversation-Id (absent
= a new conversation), X-Chat-Type (live, reply, create, spellcheck).
For a known conversation the server takes only the new messages from the
request: the last user message, or the trailing tool results.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import agent
from .auth import ai_allowed, bearer
from .tools import DEFINITIONS

router = APIRouter()

CHAT_TYPES = ("live", "reply", "create", "spellcheck")
MAX_TEXT = 200_000
MAX_MESSAGES = 1000
TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def chat_error(code: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": code}}, status_code=code, headers=headers)


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def parse_client_tools(raw, max_tools: int = 32, max_bytes: int = 32768) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tools must be a list")
    if len(raw) > max_tools:
        raise ValueError(f"too many tools (maximum {max_tools})")
    out = []
    for t in raw:
        fn = t.get("function") if isinstance(t, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if t.get("type") != "function" or not isinstance(name, str) or not name:
            raise ValueError("each tool must be {type: function, function: {name, ...}}")
        if not TOOL_NAME.match(name):
            raise ValueError("a tool name must be 1 to 64 letters, digits, _ or -")
        if name in DEFINITIONS:
            raise ValueError(f"the tool name {name} is reserved by the server")
        out.append({"type": "function", "function": {
            "name": name, "description": str(fn.get("description", "")),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        }})
    if len(json.dumps(out)) > max_bytes:
        raise ValueError(f"the tool definitions are too large (maximum {max_bytes} bytes)")
    return out


def new_turns(messages: list, stored: list[dict]) -> list[dict]:
    """The new part of a continued conversation."""
    tail = []
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "tool":
            tail.append(m)
        else:
            break
    if tail:
        pending = agent.pending_calls(stored)
        out = []
        for m in reversed(tail):
            cid = m.get("tool_call_id")
            if cid in pending:
                pending.remove(cid)
                out.append({"role": "tool", "tool_call_id": cid, "content": text_of(m.get("content"))[:20000]})
        if not out:
            raise ValueError("the tool results do not match an open tool call")
        return out
    last = messages[-1] if messages else None
    text = text_of(last.get("content")) if isinstance(last, dict) and last.get("role") == "user" else ""
    if not text.strip():
        raise ValueError("the last message must be a user message or tool results")
    return [{"role": "user", "content": text[:MAX_TEXT]}]


async def _stream(queue: asyncio.Queue, job: asyncio.Task, ping: float):
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), ping)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                return
            yield item
    finally:
        if not job.done():          # the client went away: stop the agent
            job.cancel()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    st = request.app.state
    db, cfg = st.db, st.cfg

    box = db.session_mailbox(bearer(request))
    if not box:
        return chat_error(401, "The session expired. Please log in again.")
    if not ai_allowed(db, cfg.access, box):
        return chat_error(403, "AI is not enabled for this mailbox.")
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError
    except ValueError:
        return chat_error(400, "The request body is not valid JSON.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return chat_error(400, "messages is required.")
    if len(messages) > MAX_MESSAGES:
        return chat_error(400, "Too many messages. Start a new conversation.")
    try:
        client_tools = parse_client_tools(body.get("tools"), cfg.agent.max_tools, cfg.agent.max_tool_bytes)
    except (ValueError, AttributeError) as e:
        return chat_error(400, str(e))

    conv = request.headers.get("X-Conversation-Id", "").strip()
    chat_type = request.headers.get("X-Chat-Type", "").strip()
    if chat_type and chat_type not in CHAT_TYPES:
        return chat_error(400, f"Unknown chat type {chat_type}.")
    if st.active >= cfg.agent.max_concurrent:
        return chat_error(429, "The agent is busy. Try again later.", {"Retry-After": "30"})
    if st.active_by_box.get(box["id"], 0) >= cfg.agent.max_per_mailbox:
        return chat_error(429, "The agent is already working for you. Wait for the answer or stop it.",
                          {"Retry-After": "10"})
    if cfg.agent.daily_tokens:
        today = datetime.now().strftime("%Y-%m-%d 00:00:00")
        if db.tokens_since(box["id"], today) >= cfg.agent.daily_tokens:
            return chat_error(429, "You reached your daily AI limit. Try again tomorrow.")

    if conv:
        try:
            at_id = int(conv)
        except ValueError:
            return chat_error(400, "Invalid X-Conversation-Id.")
        task = db.task_get(at_id, box["id"])
        if not task or task["type"] not in CHAT_TYPES:
            return chat_error(404, "The conversation does not exist.")
        if chat_type and chat_type != task["type"]:
            return chat_error(400, "The chat type of a conversation cannot change.")
        chat_type = task["type"]
        if db.turn_count(at_id) >= cfg.agent.max_turns:
            return chat_error(400, "This conversation is too long. Start a new conversation.")
        stored = db.turns_get(at_id)
        if not stored:
            return chat_error(400, "This conversation has no stored history.")
        try:
            turns = new_turns(messages, stored)
        except ValueError as e:
            return chat_error(400, str(e))
        if not db.task_start(at_id, box["id"]):
            return chat_error(409, "This conversation is being answered right now.")
        names = set(json.loads(task["client_tools"] or "[]"))
    else:
        chat_type = chat_type or "live"
        try:
            turns = new_turns(messages[-1:], [])
        except ValueError as e:
            return chat_error(400, str(e))
        at_id = db.task_create(box["id"], chat_type, title=turns[0]["content"].strip()[:100], state="running")
        names = set()

    names |= {t["function"]["name"] for t in client_tools}
    db.task_update(at_id, client_tools=json.dumps(sorted(names)))
    for t in turns:
        db.turn_add(at_id, t["role"], t["content"], tool_call_id=t.get("tool_call_id"))

    queue: asyncio.Queue = asyncio.Queue()
    st.active += 1                  # agent.run() counts both down
    st.active_by_box[box["id"]] = st.active_by_box.get(box["id"], 0) + 1
    job = asyncio.create_task(agent.run(st, at_id, box["id"], chat_type, client_tools, queue.put_nowait))
    st.jobs.add(job)
    job.add_done_callback(st.jobs.discard)
    return StreamingResponse(
        _stream(queue, job, cfg.agent.ping_interval),
        media_type="text/event-stream",
        headers={"X-Conversation-Id": str(at_id), "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
