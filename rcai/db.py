# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""SQLite storage: domains, mailboxes, login sessions, tasks and turns.

One connection, guarded by a lock. Every statement is short, so the
async code calls these methods directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime

INSTRUCTION_TYPES = ("incoming", "summary_daily", "summary_weekly", "reply", "create", "spellcheck")
TASK_TYPES = ("incoming", "summary_daily", "summary_weekly", "live", "reply", "create", "spellcheck")

# ai_enable: 1 = on, 0 = off, NULL = the [access] default of the config.
MAILBOX_TABLE = """
CREATE TABLE IF NOT EXISTS mailbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL UNIQUE,
    address TEXT NOT NULL,
    fullname TEXT NOT NULL DEFAULT '',
    ai_enable INTEGER,
    enable_incoming INTEGER NOT NULL DEFAULT 0,
    enable_summary_daily INTEGER NOT NULL DEFAULT 0,
    enable_summary_weekly INTEGER NOT NULL DEFAULT 0,
    summary_daily_next_run TEXT,
    summary_weekly_next_run TEXT,
    summary_daily_last_run TEXT,
    summary_weekly_last_run TEXT,
    instr_incoming TEXT NOT NULL DEFAULT '',
    instr_summary_daily TEXT NOT NULL DEFAULT '',
    instr_summary_weekly TEXT NOT NULL DEFAULT '',
    instr_reply TEXT NOT NULL DEFAULT '',
    instr_create TEXT NOT NULL DEFAULT '',
    instr_spellcheck TEXT NOT NULL DEFAULT '',
    memory TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);
"""

# A domain without a row follows the [access] default of the config.
SCHEMA = """
CREATE TABLE IF NOT EXISTS domain (
    name TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
);
""" + MAILBOX_TABLE + """
CREATE TABLE IF NOT EXISTS session (
    token_hash TEXT PRIMARY KEY,
    mailbox_id INTEGER NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    created TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task (
    at_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox_id INTEGER NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'new',
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    folder TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    client_tools TEXT NOT NULL DEFAULT '[]',
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS task_mailbox ON task(mailbox_id, created);
CREATE TABLE IF NOT EXISTS turn (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_id INTEGER NOT NULL REFERENCES task(at_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,
    tool_call_id TEXT
);
CREATE INDEX IF NOT EXISTS turn_task ON turn(at_id, id);
"""

MAILBOX_COLUMNS = {
    "fullname", "ai_enable", "enable_incoming", "enable_summary_daily", "enable_summary_weekly",
    "summary_daily_next_run", "summary_weekly_next_run", "summary_daily_last_run",
    "summary_weekly_last_run", "memory", *(f"instr_{t}" for t in INSTRUCTION_TYPES),
}
TASK_COLUMNS = {"state", "started", "finished", "folder", "result", "error", "title", "client_tools", "deleted"}


def now() -> str:
    """Local server time, the format the plugin shows."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._c.row_factory = sqlite3.Row
        self._c.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._c.execute("PRAGMA journal_mode = WAL")
        self._c.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Old databases: mailbox.ai_enable was NOT NULL DEFAULT 0.

        SQLite cannot drop a NOT NULL constraint, so copy the table. The
        old value 0 meant "not enabled", so it becomes NULL (the default).
        """
        cols = {r["name"]: r for r in self._all("PRAGMA table_info(mailbox)")}
        if not cols["ai_enable"]["notnull"]:
            return
        names = ", ".join(cols)
        with self._lock:
            # Without this, DROP TABLE deletes the sessions and tasks (ON DELETE CASCADE).
            self._c.execute("PRAGMA foreign_keys = OFF")
            try:
                self._c.execute("BEGIN")
                self._c.execute(MAILBOX_TABLE.replace("CREATE TABLE IF NOT EXISTS mailbox", "CREATE TABLE mailbox_new"))
                self._c.execute(f"INSERT INTO mailbox_new ({names}) SELECT {names} FROM mailbox")
                self._c.execute("UPDATE mailbox_new SET ai_enable = NULL WHERE ai_enable = 0")
                self._c.execute("DROP TABLE mailbox")
                self._c.execute("ALTER TABLE mailbox_new RENAME TO mailbox")
                self._c.execute("COMMIT")
            except BaseException:
                self._c.execute("ROLLBACK")
                raise
            finally:
                self._c.execute("PRAGMA foreign_keys = ON")

    # -- helpers ---------------------------------------------------------
    def _all(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._c.execute(sql, args).fetchall()]

    def _one(self, sql: str, args=()) -> dict | None:
        with self._lock:
            r = self._c.execute(sql, args).fetchone()
            return dict(r) if r else None

    def _run(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            return self._c.execute(sql, args)

    def close(self) -> None:
        with self._lock:
            self._c.close()

    def recover(self) -> None:
        """After a restart no agent runs: close what was left running."""
        self._run("UPDATE task SET state = 'error', error = 'interrupted (backend restart)', finished = ? "
                  "WHERE state = 'running'", (now(),))

    # -- domains ---------------------------------------------------------
    def domain_state(self, name: str) -> bool | None:
        """True or False if an administrator set the domain, else None."""
        r = self._one("SELECT enabled FROM domain WHERE name = ?", (name.lower(),))
        return None if r is None else bool(r["enabled"])

    def domain_set(self, name: str, enabled: bool) -> None:
        self._run("INSERT INTO domain (name, enabled) VALUES (?, ?) "
                  "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled", (name.lower(), int(enabled)))

    def domain_delete(self, name: str) -> None:
        self._run("DELETE FROM domain WHERE name = ?", (name.lower(),))

    def domain_list(self) -> list[dict]:
        return self._all("SELECT * FROM domain ORDER BY name")

    # -- mailboxes -------------------------------------------------------
    def mailbox_get(self, login: str) -> dict | None:
        return self._one("SELECT * FROM mailbox WHERE login = ?", (login.lower(),))

    def mailbox_by_id(self, mailbox_id: int) -> dict | None:
        return self._one("SELECT * FROM mailbox WHERE id = ?", (mailbox_id,))

    def mailbox_get_or_create(self, login: str, address: str) -> dict:
        login = login.lower()
        with self._lock:
            self._run("INSERT OR IGNORE INTO mailbox (login, address, created) VALUES (?, ?, ?)",
                      (login, address.lower(), now()))
            return self.mailbox_get(login)

    def mailbox_update(self, mailbox_id: int, values: dict) -> None:
        bad = set(values) - MAILBOX_COLUMNS
        if bad:
            raise ValueError(f"unknown mailbox columns: {bad}")
        if values:
            cols = ", ".join(f"{k} = ?" for k in values)
            self._run(f"UPDATE mailbox SET {cols} WHERE id = ?", (*values.values(), mailbox_id))

    def mailbox_list(self) -> list[dict]:
        return self._all("SELECT * FROM mailbox ORDER BY login")

    # -- login sessions --------------------------------------------------
    def session_create(self, mailbox_id: int, ttl: int) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._run("DELETE FROM session WHERE expires < ?", (time.time(),))
            self._run("INSERT INTO session (token_hash, mailbox_id, created, expires) VALUES (?, ?, ?, ?)",
                      (_hash(token), mailbox_id, now(), time.time() + ttl))
        return token

    def session_mailbox(self, token: str) -> dict | None:
        """The mailbox of a valid token, or None."""
        if not token:
            return None
        return self._one("SELECT m.* FROM session s JOIN mailbox m ON m.id = s.mailbox_id "
                         "WHERE s.token_hash = ? AND s.expires > ?", (_hash(token), time.time()))

    def session_delete(self, token: str) -> None:
        self._run("DELETE FROM session WHERE token_hash = ?", (_hash(token),))

    # -- tasks -----------------------------------------------------------
    def task_create(self, mailbox_id: int, type_: str, *, title: str = "", state: str = "new",
                    folder: str = "") -> int:
        t = now()
        cur = self._run("INSERT INTO task (mailbox_id, type, state, created, started, title, folder) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (mailbox_id, type_, state, t, t if state == "running" else None, title, folder))
        return cur.lastrowid

    def task_get(self, at_id: int, mailbox_id: int) -> dict | None:
        return self._one("SELECT * FROM task WHERE at_id = ? AND mailbox_id = ? AND deleted = 0",
                         (at_id, mailbox_id))

    def task_start(self, at_id: int, mailbox_id: int) -> bool:
        """Mark a task running. False when it runs already."""
        cur = self._run("UPDATE task SET state = 'running', started = ?, error = '' "
                        "WHERE at_id = ? AND mailbox_id = ? AND state != 'running'", (now(), at_id, mailbox_id))
        return cur.rowcount == 1

    def task_update(self, at_id: int, **values) -> None:
        bad = set(values) - TASK_COLUMNS
        if bad:
            raise ValueError(f"unknown task columns: {bad}")
        if values:
            cols = ", ".join(f"{k} = ?" for k in values)
            self._run(f"UPDATE task SET {cols} WHERE at_id = ?", (*values.values(), at_id))

    def tokens_since(self, mailbox_id: int, since: str) -> int:
        """Tokens of the tasks that a mailbox created since a now() time."""
        row = self._one("SELECT COALESCE(SUM(tokens_in + tokens_out), 0) AS n FROM task "
                        "WHERE mailbox_id = ? AND created >= ?", (mailbox_id, since))
        return row["n"] if row else 0

    def turn_count(self, at_id: int) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM turn WHERE at_id = ?", (at_id,))
        return row["n"] if row else 0

    def task_add_usage(self, at_id: int, tokens_in: int, tokens_out: int) -> None:
        self._run("UPDATE task SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ? WHERE at_id = ?",
                  (tokens_in, tokens_out, at_id))

    def task_list(self, mailbox_id: int, types: list[str] | None = None, *, with_turns: bool = False,
                  by_activity: bool = False, limit: int = 50, offset: int = 0) -> tuple[int, list[dict]]:
        where, args = ["t.mailbox_id = ?", "t.deleted = 0"], [mailbox_id]
        if types:
            where.append(f"t.type IN ({', '.join('?' * len(types))})")
            args += types
        if with_turns:
            where.append("EXISTS (SELECT 1 FROM turn u WHERE u.at_id = t.at_id)")
        w = " AND ".join(where)
        order = "COALESCE(t.finished, t.started, t.created) DESC, t.at_id DESC" if by_activity else "t.at_id DESC"
        total = self._one(f"SELECT COUNT(*) AS n FROM task t WHERE {w}", args)["n"]
        rows = self._all(f"SELECT t.* FROM task t WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?",
                         [*args, limit, offset])
        return total, rows

    def usage_months(self, mailbox_id: int, months: int) -> list[dict]:
        """Monthly sums, deleted tasks included. reply, create and spellcheck count as live."""
        today = date.today()
        y, m = today.year, today.month - (months - 1)
        while m < 1:
            m += 12
            y -= 1
        rows = self._all("SELECT substr(created, 1, 7) AS month, type, COUNT(*) AS n, "
                         "SUM(tokens_in) AS tin, SUM(tokens_out) AS tout FROM task "
                         "WHERE mailbox_id = ? AND substr(created, 1, 7) >= ? GROUP BY month, type",
                         (mailbox_id, f"{y:04d}-{m:02d}"))
        group = {"incoming": "incoming", "summary_daily": "summary", "summary_weekly": "summary"}
        out: dict[str, dict] = {}
        for r in rows:
            mo = out.setdefault(r["month"], {"month": r["month"], **{
                k: {"tasks": 0, "tokens_in": 0, "tokens_out": 0} for k in ("live", "incoming", "summary", "total")}})
            for k in (group.get(r["type"], "live"), "total"):
                mo[k]["tasks"] += r["n"]
                mo[k]["tokens_in"] += r["tin"] or 0
                mo[k]["tokens_out"] += r["tout"] or 0
        return sorted(out.values(), key=lambda x: x["month"], reverse=True)

    # -- conversation turns ----------------------------------------------
    def turn_add(self, at_id: int, role: str, content: str = "", *, tool_calls: list | None = None,
                 tool_call_id: str | None = None) -> None:
        self._run("INSERT INTO turn (at_id, role, content, tool_calls, tool_call_id) VALUES (?, ?, ?, ?, ?)",
                  (at_id, role, content or "", json.dumps(tool_calls) if tool_calls else None, tool_call_id))

    def turns_get(self, at_id: int) -> list[dict]:
        rows = self._all("SELECT role, content, tool_calls, tool_call_id FROM turn WHERE at_id = ? ORDER BY id",
                         (at_id,))
        for r in rows:
            r["tool_calls"] = json.loads(r["tool_calls"]) if r["tool_calls"] else None
        return rows
