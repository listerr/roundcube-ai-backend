# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""Tools that the server runs itself, over the mailbox (IMAP) and the memory.

The client (the plugin in the browser) sends its own tools in the request.
The agent offers both sets to the model, but runs only these.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from .db import Database
from .mail import MailError, MailUnavailable

log = logging.getLogger(__name__)

MEMORY_MAX = 20000


def _fn(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required, "additionalProperties": False},
    }}


_FOLDER = {"type": "string", "description": "Folder of the message, e.g. INBOX."}
_UID = {"type": "integer", "description": "UID of the message in the folder."}

DEFINITIONS = {d["function"]["name"]: d for d in (
    _fn("mail_list_folders", "Lists the folders of the mailbox, with their special use (Sent, Junk, Trash ...).",
        {"counts": {"type": "boolean", "description": "Also return the message and unread counts (slower)."}}, []),
    _fn("mail_search",
        "Searches messages. All given criteria must match. Without a folder it searches all folders. "
        "Returns the newest matches first, with folder, UID, date, from, to and subject.",
        {
            "folder": {"type": "string", "description": "Folder to search. Leave out to search all folders."},
            "from": {"type": "string", "description": "Text in the From address or name."},
            "to": {"type": "string", "description": "Text in the To address or name."},
            "subject": {"type": "string", "description": "Text in the subject."},
            "text": {"type": "string", "description": "Text anywhere in the headers or the body."},
            "since": {"type": "string", "description": "Received on or after this date, YYYY-MM-DD."},
            "before": {"type": "string", "description": "Received before this date, YYYY-MM-DD."},
            "unseen": {"type": "boolean", "description": "Only unread messages."},
            "flagged": {"type": "boolean", "description": "Only flagged messages."},
            "limit": {"type": "integer", "description": "Maximum number of messages to return (default 20)."},
        }, []),
    _fn("mail_read", "Reads one message: headers, text and the list of attachments. Does not mark it as read.",
        {"folder": _FOLDER, "uid": _UID}, ["folder", "uid"]),
    _fn("mail_read_attachment", "Reads the text of one attachment (text, HTML, attached message, PDF).",
        {"folder": _FOLDER, "uid": _UID,
         "index": {"type": "integer", "description": "Index of the attachment, from mail_read."}},
        ["folder", "uid", "index"]),
    _fn("mail_move", "Moves a message to another folder.",
        {"folder": _FOLDER, "uid": _UID, "to_folder": {"type": "string", "description": "Target folder."}},
        ["folder", "uid", "to_folder"]),
    _fn("mail_copy", "Copies a message to another folder.",
        {"folder": _FOLDER, "uid": _UID, "to_folder": {"type": "string", "description": "Target folder."}},
        ["folder", "uid", "to_folder"]),
    _fn("mail_create_folder", "Creates a folder. For a subfolder, use the separator that the existing folder names use.",
        {"name": {"type": "string", "description": "Full name of the new folder."}}, ["name"]),
    _fn("memory_update", "Replaces your memory of this user with a new text. Give the whole memory, not only the change.",
        {"memory": {"type": "string", "description": "The whole new memory."}}, ["memory"]),
)}

READ_TOOLS = ["mail_list_folders", "mail_search", "mail_read", "mail_read_attachment"]
TYPE_TOOLS = {
    "live": [*READ_TOOLS, "mail_move", "mail_copy", "mail_create_folder", "memory_update"],
    "reply": READ_TOOLS,
    "create": READ_TOOLS,
    "spellcheck": [],
}


def definitions(chat_type: str) -> list[dict]:
    return [DEFINITIONS[n] for n in TYPE_TOOLS[chat_type]]


@dataclass
class ToolContext:
    db: Database
    mailbox_id: int
    mail: object                # an ImapSession (or a test double)
    search_limit: int = 100         # 0 = no limit
    read_max_chars: int = 30000

    def reset_mail(self) -> None:
        try:
            self.mail.close()
        except Exception:  # noqa: BLE001
            pass


def _str(args: dict, key: str) -> str:
    v = args.get(key)
    return v.strip() if isinstance(v, str) else ""


async def _dispatch(name: str, a: dict, ctx: ToolContext):
    m = ctx.mail
    run = asyncio.to_thread
    if name == "mail_list_folders":
        return await run(m.folders, bool(a.get("counts")))
    if name == "mail_search":
        limit = max(1, int(a.get("limit") or 20))
        if ctx.search_limit:
            limit = min(limit, ctx.search_limit)
        return await run(m.search, folder=_str(a, "folder") or None, from_=_str(a, "from"), to=_str(a, "to"),
                         subject=_str(a, "subject"), text=_str(a, "text"), since=_str(a, "since"),
                         before=_str(a, "before"), unseen=bool(a.get("unseen")), flagged=bool(a.get("flagged")),
                         limit=limit)
    if name == "mail_read":
        return await run(m.read, _str(a, "folder"), int(a["uid"]), ctx.read_max_chars)
    if name == "mail_read_attachment":
        return await run(m.attachment, _str(a, "folder"), int(a["uid"]), int(a["index"]), ctx.read_max_chars)
    if name == "mail_move":
        return await run(m.move, _str(a, "folder"), int(a["uid"]), _str(a, "to_folder"))
    if name == "mail_copy":
        return await run(m.copy, _str(a, "folder"), int(a["uid"]), _str(a, "to_folder"))
    if name == "mail_create_folder":
        if not _str(a, "name"):
            raise MailError("name is required")
        return await run(m.create_folder, _str(a, "name"))
    if name == "memory_update":
        text = str(a.get("memory", "")).strip()
        if len(text) > MEMORY_MAX:
            raise MailError(f"the memory is too long ({len(text)} characters, maximum {MEMORY_MAX})")
        ctx.db.mailbox_update(ctx.mailbox_id, {"memory": text})
        return "OK: memory saved"
    raise MailError(f"unknown tool {name}")


async def run(name: str, arguments: str, ctx: ToolContext) -> str:
    """Runs one tool call. Returns the text for the model; errors start with "ERROR:"."""
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        return "ERROR: the arguments are not valid JSON"
    try:
        result = await _dispatch(name, args, ctx)
    except MailError as e:
        return f"ERROR: {e}"
    except (KeyError, TypeError, ValueError) as e:
        return f"ERROR: invalid arguments: {e}"
    except MailUnavailable as e:
        ctx.reset_mail()
        return f"ERROR: {e}"
    except Exception as e:  # noqa: BLE001 - IMAP and socket errors: reconnect on the next call
        log.warning("tool %s failed: %s: %s", name, type(e).__name__, e)
        ctx.reset_mail()
        return f"ERROR: {type(e).__name__}: {e}"
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
