# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""Tokens, the login rate limit and the AI access check."""

from __future__ import annotations

import time
from collections import deque

from fastapi import Request

from .config import AccessConfig
from .db import Database, domain_of


def bearer(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def ai_default(access: AccessConfig, domain: str) -> bool:
    """The [access] default for a domain, when the database has no setting."""
    return access.enable_all or domain.lower() in access.enable_domains


def domain_ai(db: Database, access: AccessConfig, domain: str) -> bool:
    state = db.domain_state(domain)
    return ai_default(access, domain) if state is None else state


def mailbox_ai(access: AccessConfig, box: dict) -> bool:
    """The mailbox switch only; ai_allowed also checks the domain."""
    if box["ai_enable"] is None:
        return ai_default(access, domain_of(box["address"]))
    return bool(box["ai_enable"])


def ai_allowed(db: Database, access: AccessConfig, box: dict) -> bool:
    return mailbox_ai(access, box) and domain_ai(db, access, domain_of(box["address"]))


def address_for(login: str, default_domain: str) -> str:
    if "@" in login or not default_domain:
        return login
    return f"{login}@{default_domain}"


class LoginLimiter:
    """Counts failed passwords per (login, client address) and per client address.

    A login name alone is not a key, so an attacker cannot lock out a user
    from all addresses. The table is bounded: expired entries go at most
    every 60 seconds, and at MAX_KEYS the oldest entries go.
    """

    MAX_KEYS = 50_000

    def __init__(self, max_failures: int, max_failures_ip: int, window: int):
        self.max, self.max_ip, self.window = max_failures, max_failures_ip, window
        self._fails: dict[tuple, deque] = {}
        self._swept = time.monotonic()

    def _recent(self, key: tuple) -> int:
        q = self._fails.get(key)
        if not q:
            return 0
        limit = time.monotonic() - self.window
        while q and q[0] < limit:
            q.popleft()
        if not q:
            del self._fails[key]
            return 0
        return len(q)

    def _sweep(self) -> None:
        t = time.monotonic()
        if t - self._swept >= 60:
            self._swept = t
            for key in list(self._fails):
                self._recent(key)
        while len(self._fails) >= self.MAX_KEYS:
            del self._fails[next(iter(self._fails))]      # the oldest key

    def blocked(self, login: str, ip: str) -> bool:
        return (self._recent(("ip", ip)) >= self.max_ip
                or self._recent(("login", login.lower(), ip)) >= self.max)

    def fail(self, login: str, ip: str) -> None:
        self._sweep()
        t = time.monotonic()
        for key in (("ip", ip), ("login", login.lower(), ip)):
            self._fails.setdefault(key, deque()).append(t)

    def clear(self, login: str, ip: str) -> None:
        self._fails.pop(("login", login.lower(), ip), None)

    def __len__(self) -> int:
        return len(self._fails)


def client_ip(request: Request) -> str:
    """The client address. Uvicorn takes it from X-Forwarded-For only when the
    proxy is in [server] forwarded_allow_ips."""
    return request.client.host if request.client else ""
