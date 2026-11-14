# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""The agent loop for one chat request.

The loop calls the model, runs the server tools, and repeats until the
model gives an answer or calls a client tool. Client tool calls go back to
the browser with finish_reason "tool_calls"; the browser sends the results
in the next request of the same conversation.

Each model step is a normal (not streaming) request. While the agent works,
the stream carries only ": ping" lines. The answer is sent at the end, in
chunks, as the plugin expects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable

import openai

from . import prompts, tools
from .db import Database, now

log = logging.getLogger(__name__)

INTERRUPTED = "ERROR: no result, the call was interrupted"
CHUNK = 200


def pending_calls(turns: list[dict]) -> list[str]:
    """IDs of the tool calls of the last assistant turn that have no result yet."""
    pending: list[str] = []
    for t in turns:
        if t["role"] == "assistant":
            pending = [c["id"] for c in t["tool_calls"] or []]
        elif t["role"] == "tool" and t["tool_call_id"] in pending:
            pending.remove(t["tool_call_id"])
        elif t["role"] == "user":
            pending = []
    return pending


def build_history(turns: list[dict]) -> list[dict]:
    """Stored turns as OpenAI messages. A call without a result gets an error
    result, so that the model accepts the history."""
    out: list[dict] = []
    pending: list[str] = []

    def flush():
        for cid in pending:
            out.append({"role": "tool", "tool_call_id": cid, "content": INTERRUPTED})
        pending.clear()

    for t in turns:
        if t["role"] == "tool":
            if t["tool_call_id"] in pending:
                pending.remove(t["tool_call_id"])
                out.append({"role": "tool", "tool_call_id": t["tool_call_id"], "content": t["content"]})
            continue
        flush()
        if t["role"] == "assistant" and t["tool_calls"]:
            out.append({"role": "assistant", "content": t["content"] or None, "tool_calls": t["tool_calls"]})
            pending.extend(c["id"] for c in t["tool_calls"])
        else:
            out.append({"role": t["role"], "content": t["content"]})
    flush()
    return out


def display_turns(turns: list[dict], client_names: set[str]) -> list[dict]:
    """Turns for the chat window: user questions, answers and client tool calls.
    Steps with server tools only are internal and not shown."""
    out, shown_ids = [], set()
    for t in turns:
        if t["role"] == "user":
            out.append({"role": "user", "content": t["content"]})
        elif t["role"] == "assistant":
            calls = [c for c in t["tool_calls"] or [] if c["function"]["name"] in client_names]
            if calls:
                shown_ids.update(c["id"] for c in calls)
                out.append({"role": "assistant", "content": t["content"], "tool_calls": calls})
            elif not t["tool_calls"]:
                out.append({"role": "assistant", "content": t["content"]})
        elif t["role"] == "tool" and t["tool_call_id"] in shown_ids:
            out.append({"role": "tool", "tool_call_id": t["tool_call_id"], "content": t["content"]})
    return out


class Sse:
    """Server-sent events in the OpenAI chunk format, plus conversation_id."""

    def __init__(self, at_id: int, model: str, emit: Callable[[str | None], None]):
        self.at_id, self.model, self.emit = at_id, model, emit
        self.created = int(time.time())

    def chunk(self, delta: dict, finish: str | None = None) -> None:
        data = {
            "id": f"chatcmpl-{self.at_id}-{self.created}", "object": "chat.completion.chunk",
            "created": self.created, "model": self.model, "conversation_id": str(self.at_id),
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        self.emit("data: " + json.dumps(data, ensure_ascii=False) + "\n\n")

    def answer(self, text: str) -> None:
        self.chunk({"role": "assistant", "content": ""})
        for i in range(0, len(text), CHUNK):
            self.chunk({"content": text[i:i + CHUNK]})

    def tool_calls(self, calls: list[dict]) -> None:
        self.chunk({"tool_calls": [{"index": i, "id": c["id"], "type": "function", "function": c["function"]}
                                   for i, c in enumerate(calls)]})

    def end(self, finish: str) -> None:
        self.chunk({}, finish)
        self.emit("data: [DONE]\n\n")

    def error(self, message: str) -> None:
        self.emit("data: " + json.dumps({"error": {"message": message}}, ensure_ascii=False) + "\n\n")


def _llm_error(e: Exception) -> str:
    if isinstance(e, TimeoutError) and not isinstance(e, openai.APITimeoutError):
        return "The agent took too long and stopped. Ask a smaller question."
    if isinstance(e, openai.RateLimitError):
        return "The AI service is busy or the usage limit is reached. Try again later."
    if isinstance(e, openai.AuthenticationError):
        return "The AI service did not accept the backend's API key."
    if isinstance(e, (openai.APITimeoutError, openai.APIConnectionError)):
        return "The AI service is not reachable. Try again later."
    if isinstance(e, openai.APIStatusError):
        return f"The AI service returned an error ({e.status_code})."
    return "The agent failed. The administrator can find the cause in the backend log."


async def run(state, at_id: int, mailbox_id: int, chat_type: str, client_tools: list[dict],
              emit: Callable[[str | None], None]) -> None:
    """Runs the agent for one request. emit() gets SSE text, and None at the end."""
    cfg, db = state.cfg, state.db
    box = db.mailbox_by_id(mailbox_id)
    sse = Sse(at_id, cfg.openai.model, emit)
    server_defs = tools.definitions(chat_type)
    mail = state.mail.session(box["login"]) if server_defs else None
    ctx = tools.ToolContext(db, mailbox_id, mail, cfg.agent.search_limit, cfg.agent.read_max_chars)
    try:
        async with asyncio.timeout(cfg.agent.run_timeout):
            await _loop(state, db, at_id, box, chat_type, server_defs, client_tools, ctx, sse)
    except asyncio.CancelledError:
        # The user pressed stop or the connection closed.
        db.task_update(at_id, state="done", finished=now(), error="stopped")
        raise
    except Exception as e:  # noqa: BLE001 - every failure must end the stream and the task
        if isinstance(e, openai.OpenAIError):
            log.warning("task %s: OpenAI: %s", at_id, e)
        elif isinstance(e, TimeoutError):
            log.warning("task %s: stopped after run_timeout (%s s)", at_id, cfg.agent.run_timeout)
        else:
            log.exception("task %s failed", at_id)
        msg = _llm_error(e)
        db.task_update(at_id, state="error", finished=now(), error=msg)
        sse.error(msg)
    finally:
        if mail is not None:
            ctx.reset_mail()
        state.active -= 1
        n = state.active_by_box.get(mailbox_id, 1) - 1
        if n > 0:
            state.active_by_box[mailbox_id] = n
        else:
            state.active_by_box.pop(mailbox_id, None)
        emit(None)          # last statement: the stream ends after the task is done


async def _loop(state, db: Database, at_id: int, box: dict, chat_type: str, server_defs: list[dict],
                client_tools: list[dict], ctx: tools.ToolContext, sse: Sse) -> None:
    cfg = state.cfg
    system = prompts.system_prompt(chat_type, box, cfg.agent.timezone)
    server_names = {d["function"]["name"] for d in server_defs}
    client_names = {d["function"]["name"] for d in client_tools}
    all_tools = server_defs + client_tools
    history = build_history(db.turns_get(at_id))

    for step in range(cfg.agent.max_steps):
        req = {"model": cfg.openai.model, "messages": [{"role": "system", "content": system}, *history],
               **cfg.openai.params}
        if all_tools:
            req["tools"] = all_tools
            if step == cfg.agent.max_steps - 1:
                req["tool_choice"] = "none"      # last step: the model must answer
        resp = await state.llm.chat.completions.create(**req)
        if resp.usage:
            db.task_add_usage(at_id, resp.usage.prompt_tokens or 0, resp.usage.completion_tokens or 0)
        msg = resp.choices[0].message
        content = msg.content or ""
        calls = [{"id": c.id, "type": "function",
                  "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                 for c in msg.tool_calls or [] if getattr(c, "function", None) is not None]

        if not calls:
            content = content.strip()
            db.turn_add(at_id, "assistant", content)
            db.task_update(at_id, state="done", finished=now(), result=content[:500])
            sse.answer(content)
            sse.end("stop")
            return

        db.turn_add(at_id, "assistant", content, tool_calls=calls)
        history.append({"role": "assistant", "content": content or None, "tool_calls": calls})
        mine = [c for c in calls if c["function"]["name"] in client_names]
        for c in calls:
            if c in mine:
                continue
            name = c["function"]["name"]
            t0 = time.monotonic()
            if name in server_names:
                result = await tools.run(name, c["function"]["arguments"], ctx)
            else:
                result = f"ERROR: unknown tool {name}"
            log.info("task %s: tool %s %.1fs%s", at_id, name, time.monotonic() - t0,
                     " (error)" if result.startswith("ERROR") else "")
            db.turn_add(at_id, "tool", result, tool_call_id=c["id"])
            history.append({"role": "tool", "tool_call_id": c["id"], "content": result})

        if mine:
            db.task_update(at_id, state="done", finished=now(), result=(content.strip() or "(tool calls)")[:500])
            if content.strip():
                sse.answer(content.strip())
            sse.tool_calls(mine)
            sse.end("tool_calls")
            return

    # Not reached: the last step has tool_choice "none". A model that still
    # calls tools ends here.
    text = "I stopped because the task needed too many steps. Please make the request more specific."
    db.turn_add(at_id, "assistant", text)
    db.task_update(at_id, state="done", finished=now(), result=text)
    sse.answer(text)
    sse.end("stop")
