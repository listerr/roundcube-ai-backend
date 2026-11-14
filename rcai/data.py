# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""The data API: GET|POST <base>/?fn=<operation>.

POST bodies are application/x-www-form-urlencoded. Responses are JSON. An
error is an HTTP status and {"error": "<text for the user>", "code": N}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import parse_qs

from fastapi import APIRouter, Request

from . import agent
from .auth import address_for, bearer, client_ip, domain_ai, mailbox_ai
from .db import INSTRUCTION_TYPES, TASK_TYPES, domain_of
from .mail import MailUnavailable
from .prompts import DEFAULT_INSTRUCTIONS

log = logging.getLogger(__name__)
router = APIRouter()

CHAT_LIST_TYPES = ["live", "reply", "create"]
SWITCHES = {f"ai_enable_{k}": f"enable_{k}" for k in ("incoming", "summary_daily", "summary_weekly")}
MAX_INSTRUCTIONS = 10_000
MAX_MEMORY = 20_000


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _auth(request: Request) -> dict:
    box = request.app.state.db.session_mailbox(bearer(request))
    if not box:
        raise ApiError(401, "The session expired. Please log in again.")
    return box


def _int(p: dict, key: str, default: int, lo: int, hi: int) -> int:
    raw = p.get(key, "")
    if raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ApiError(400, f"{key} must be a number") from None
    if not lo <= v <= hi:
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return v


# -- login ---------------------------------------------------------------

async def do_login(request: Request, p: dict) -> dict:
    st = request.app.state
    login, password = p.get("login", "").strip(), p.get("password", "")
    if not login or not password or len(login) > 255:
        raise ApiError(400, "login and password are required")
    ip = client_ip(request)
    if st.limiter.blocked(login, ip):
        log.warning("do_login: blocked %s from %s", login, ip)
        raise ApiError(429, "Too many failed logins. Try again later.")
    if st.logins_active >= st.cfg.auth.max_concurrent_logins:
        raise ApiError(429, "Too many logins at the same time. Try again later.")
    st.logins_active += 1
    try:
        ok = await asyncio.to_thread(st.mail.check_login, login, password)
    except MailUnavailable as e:
        log.error("do_login: %s", e)
        raise ApiError(503, "The mail server is not available.") from None
    finally:
        st.logins_active -= 1
    if not ok:
        st.limiter.fail(login, ip)
        log.info("do_login: failed for %s from %s", login, ip)
        raise ApiError(401, "Invalid username or password.")
    st.limiter.clear(login, ip)
    box = st.db.mailbox_get_or_create(login, address_for(login, st.cfg.auth.default_domain))
    token = st.db.session_create(box["id"], st.cfg.auth.session_ttl)
    return {"session": token, "expires_in": st.cfg.auth.session_ttl, "mu_id": box["id"],
            "login": box["login"], "address": box["address"]}


async def do_logout(request: Request, _p: dict) -> dict:
    token = bearer(request)
    if token:
        request.app.state.db.session_delete(token)
    return {"ok": True}


# -- mailbox and settings -------------------------------------------------

# mail_get_mailbox opens an IMAP session for the quota. The cache limits this.
QUOTA_CACHE_SECONDS = 60


def _quota(mail, login: str):
    s = mail.session(login)
    try:
        return s.quota()
    finally:
        s.close()


async def mailbox_json(st, box: dict) -> dict:
    limit = used = msgs = None
    cached = st.quota_cache.get(box["login"])
    if cached and time.monotonic() - cached[0] < QUOTA_CACHE_SECONDS:
        limit, used, msgs = cached[1]
    else:
        try:
            limit, used, msgs = await asyncio.to_thread(_quota, st.mail, box["login"])
            st.quota_cache[box["login"]] = (time.monotonic(), (limit, used, msgs))
        except Exception as e:  # noqa: BLE001 - the quota is optional information
            log.warning("quota of %s: %s", box["login"], e)
    return {
        "address": box["address"], "login": box["login"], "fullname": box["fullname"],
        "quota": limit, "quota_used_bytes": used, "quota_used_messages": msgs,
        "ai": {
            "domain_enabled": int(domain_ai(st.db, st.cfg.access, domain_of(box["address"]))),
            "enable": int(mailbox_ai(st.cfg.access, box)),
            "enable_incoming": box["enable_incoming"],
            "enable_summary_daily": box["enable_summary_daily"],
            "enable_summary_weekly": box["enable_summary_weekly"],
            "summary_daily_next_run": box["summary_daily_next_run"],
            "summary_weekly_next_run": box["summary_weekly_next_run"],
            "summary_daily_last_run": box["summary_daily_last_run"],
            "summary_weekly_last_run": box["summary_weekly_last_run"],
            "instructions": {t: {"value": box[f"instr_{t}"], "default": DEFAULT_INSTRUCTIONS[t]}
                             for t in INSTRUCTION_TYPES},
            "memory": box["memory"],
        },
    }


async def mail_get_mailbox(request: Request, _p: dict) -> dict:
    return {"mailbox": await mailbox_json(request.app.state, _auth(request))}


async def mail_store_mailbox(request: Request, p: dict) -> dict:
    st = request.app.state
    box = _auth(request)
    domain_ok = domain_ai(st.db, st.cfg.access, domain_of(box["address"]))
    values: dict = {}
    for key, val in p.items():
        val = val.replace("\r\n", "\n")
        if key == "fullname":
            values["fullname"] = val.strip()[:200]
            continue
        if not domain_ok:
            raise ApiError(403, "AI is not available for this domain.")
        if key in SWITCHES:
            if val not in ("0", "1"):
                raise ApiError(400, f"{key} must be 0 or 1")
            values[SWITCHES[key]] = int(val)
        elif key.startswith("ai_") and key.endswith("_instructions") and key[3:-13] in INSTRUCTION_TYPES:
            if len(val) > MAX_INSTRUCTIONS:
                raise ApiError(400, f"The instructions are too long (maximum {MAX_INSTRUCTIONS} characters).")
            t = key[3:-13]
            values[f"instr_{t}"] = "" if val.strip() == DEFAULT_INSTRUCTIONS[t] else val.strip()
        elif key == "ai_memory":
            if len(val) > MAX_MEMORY:
                raise ApiError(400, f"The memory is too long (maximum {MAX_MEMORY} characters).")
            values["memory"] = val.strip()
        else:
            raise ApiError(403, f"The field {key} cannot be changed.")
    st.db.mailbox_update(box["id"], values)
    return {"mailbox": await mailbox_json(st, st.db.mailbox_by_id(box["id"]))}


# -- tasks and conversations ----------------------------------------------

def _task_json(t: dict) -> dict:
    out = {k: t[k] for k in ("at_id", "type", "state", "created", "started", "finished",
                             "tokens_in", "tokens_out", "error")}
    out["mailbox"] = t["folder"]
    result = t["result"] or ""
    out["result"] = result if len(result) <= 200 else result[:200] + "…"
    if t["type"] in CHAT_LIST_TYPES or t["type"] == "spellcheck":
        out["title"] = t["title"]
    return out


async def ai_list_tasks(request: Request, p: dict) -> dict:
    st = request.app.state
    box = _auth(request)
    type_ = p.get("type", "")
    limit = _int(p, "limit", 50, 1, 1000)
    offset = _int(p, "offset", 0, 0, 10**9)
    if type_ == "chat":
        total, rows = st.db.task_list(box["id"], CHAT_LIST_TYPES, with_turns=True, by_activity=True,
                                      limit=limit, offset=offset)
    else:
        if type_ and type_ not in TASK_TYPES:
            raise ApiError(400, f"Unknown task type {type_}.")
        total, rows = st.db.task_list(box["id"], [type_] if type_ else None, limit=limit, offset=offset)
    return {"total": total, "tasks": [_task_json(t) for t in rows]}


async def ai_get_conversation(request: Request, p: dict) -> dict:
    st = request.app.state
    box = _auth(request)
    at_id = _int(p, "at_id", 0, 1, 2**62)
    task = st.db.task_get(at_id, box["id"])
    if not task:
        raise ApiError(404, "The conversation does not exist.")
    names = set(json.loads(task["client_tools"] or "[]"))
    return {"conversation": {
        "at_id": task["at_id"], "type": task["type"], "state": task["state"], "created": task["created"],
        "turns": agent.display_turns(st.db.turns_get(at_id), names),
    }}


async def ai_usage_months(request: Request, p: dict) -> dict:
    box = _auth(request)
    months = _int(p, "months", 12, 1, 120)
    return {"months": request.app.state.db.usage_months(box["id"], months)}


HANDLERS = {
    "do_login": (do_login, True),
    "do_logout": (do_logout, True),
    "mail_get_mailbox": (mail_get_mailbox, False),
    "mail_store_mailbox": (mail_store_mailbox, True),
    "ai_list_tasks": (ai_list_tasks, False),
    "ai_get_conversation": (ai_get_conversation, False),
    "ai_usage_months": (ai_usage_months, False),
}


@router.api_route("/", methods=["GET", "POST"])
async def data_api(request: Request):
    fn = request.query_params.get("fn", "")
    handler = HANDLERS.get(fn)
    if handler is None:
        raise ApiError(400, f"Unknown operation {fn!r}.")
    func, post_only = handler
    if post_only and request.method != "POST":
        raise ApiError(400, f"{fn} needs POST.")
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        params = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
    else:
        params = {k: v for k, v in request.query_params.items() if k != "fn"}
    return await func(request, params)
