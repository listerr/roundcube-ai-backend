# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""IMAP access to Dovecot.

check_login() checks a user's password with a normal IMAP login.
session() opens a master-user session (login*master) for one mailbox, which
the agent tools use. Both are synchronous; the async code runs them in a
thread.

Folder names are UTF-8 (IMAPClient converts modified UTF-7), the same
names that the plugin accepts.
"""

from __future__ import annotations

import email
import email.policy
import io
import re
import ssl
from datetime import date
from email.header import decode_header, make_header
from email.message import EmailMessage
from html.parser import HTMLParser

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .config import ImapConfig


MAX_FETCH_BYTES = 25 * 1024 * 1024
SPECIAL_USE = {b"\\sent", b"\\drafts", b"\\trash", b"\\junk", b"\\archive", b"\\all", b"\\flagged"}


class MailError(Exception):
    """A problem with the request (no such folder or message). The text goes to the model."""


class MailUnavailable(Exception):
    """The IMAP server cannot be reached, or the master login failed."""


def _connect(cfg: ImapConfig) -> IMAPClient:
    ctx = ssl.create_default_context()
    if not cfg.tls_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        if cfg.security == "ssl":
            return IMAPClient(cfg.host, port=cfg.port, ssl=True, ssl_context=ctx, timeout=cfg.timeout)
        c = IMAPClient(cfg.host, port=cfg.port, ssl=False, timeout=cfg.timeout)
        if cfg.security == "starttls":
            c.starttls(ctx)
        return c
    except (OSError, IMAPClientError) as e:
        raise MailUnavailable(f"cannot connect to the IMAP server: {e}") from e


def _logout(c: IMAPClient) -> None:
    try:
        c.logout()
    except Exception:  # noqa: BLE001 - the connection is gone anyway
        pass


class ImapBackend:
    def __init__(self, cfg: ImapConfig):
        self.cfg = cfg

    def check_login(self, login: str, password: str) -> bool:
        c = _connect(self.cfg)
        try:
            c.login(login, password)
            return True
        except LoginError:
            return False
        finally:
            _logout(c)

    def session(self, login: str) -> "ImapSession":
        return ImapSession(self.cfg, login)


class ImapSession:
    """A master-user IMAP session for one mailbox. It connects on first use."""

    def __init__(self, cfg: ImapConfig, login: str):
        self.cfg = cfg
        self.login = login
        self._c: IMAPClient | None = None

    def _client(self) -> IMAPClient:
        if self._c is None:
            if not self.cfg.master_user:
                raise MailUnavailable("no IMAP master user is configured")
            c = _connect(self.cfg)
            try:
                c.login(f"{self.login}{self.cfg.master_separator}{self.cfg.master_user}", self.cfg.master_password)
            except LoginError as e:
                _logout(c)
                raise MailUnavailable("the IMAP master user login failed") from e
            self._c = c
        return self._c

    def close(self) -> None:
        if self._c is not None:
            _logout(self._c)
            self._c = None

    def _select(self, folder: str, readonly: bool = True) -> IMAPClient:
        c = self._client()
        if not folder or not c.folder_exists(folder):
            raise MailError(f'folder "{folder}" does not exist')
        c.select_folder(folder, readonly=readonly)
        return c

    def _check_uid(self, c: IMAPClient, folder: str, uid: int) -> dict:
        data = c.fetch([uid], ["FLAGS", "RFC822.SIZE"])
        if uid not in data:
            raise MailError(f'message {uid} does not exist in folder "{folder}" (moved or deleted?)')
        return data[uid]

    # -- reading ---------------------------------------------------------
    def folders(self, counts: bool = False) -> list[dict]:
        c = self._client()
        out = []
        for flags, _delim, name in c.list_folders():
            low = {f.lower() for f in flags}
            if b"\\noselect" in low or b"\\nonexistent" in low:
                continue
            item: dict = {"name": name}
            special = [f.decode()[1:] for f in flags if f.lower() in SPECIAL_USE]
            if special:
                item["special"] = special[0]
            if counts:
                try:
                    st = c.folder_status(name, ["MESSAGES", "UNSEEN"])
                    item["messages"], item["unseen"] = st.get(b"MESSAGES"), st.get(b"UNSEEN")
                except IMAPClientError:
                    pass
            out.append(item)
        return out

    def quota(self) -> tuple[int | None, int | None, int | None]:
        """(limit bytes, used bytes, used messages); None where the server has no value."""
        c = self._client()
        if not c.has_capability("QUOTA"):
            return None, None, None
        try:
            _roots, quotas = c.get_quota_root("INBOX")
        except IMAPClientError:
            return None, None, None
        limit = used = msgs = None
        for q in quotas:
            res = q.resource.decode() if isinstance(q.resource, bytes) else str(q.resource)
            if res.upper() == "STORAGE":
                used, limit = q.usage * 1024, q.limit * 1024
            elif res.upper() == "MESSAGE":
                msgs = q.usage
        return limit, used, msgs

    def search(self, folder: str | None = None, from_: str = "", to: str = "", subject: str = "", text: str = "",
               since: str = "", before: str = "", unseen: bool = False, flagged: bool = False,
               limit: int = 20) -> dict:
        crit: list = []
        for key, val in (("FROM", from_), ("TO", to), ("SUBJECT", subject), ("TEXT", text)):
            if val:
                crit += [key, val]
        if since:
            crit += ["SINCE", _date(since)]
        if before:
            crit += ["BEFORE", _date(before)]
        if unseen:
            crit.append("UNSEEN")
        if flagged:
            crit.append("FLAGGED")
        if not crit:
            crit = ["ALL"]
        charset = "UTF-8" if any(isinstance(v, str) and not v.isascii() for v in crit) else None

        c = self._client()
        names = [folder] if folder else [f["name"] for f in self.folders()]
        found, total = [], 0
        for name in names:
            self._select(name)
            uids = c.search(crit, charset=charset)
            total += len(uids)
            uids = sorted(uids)[-limit:]
            if not uids:
                continue
            for uid, d in c.fetch(uids, ["ENVELOPE", "FLAGS", "RFC822.SIZE", "INTERNALDATE"]).items():
                found.append(_summary(name, uid, d))
        found.sort(key=lambda m: m["date"], reverse=True)
        found = found[:limit]
        return {"total": total, "shown": len(found), "messages": found}

    def _message(self, folder: str, uid: int) -> EmailMessage:
        c = self._select(folder)
        meta = self._check_uid(c, folder, uid)
        if meta.get(b"RFC822.SIZE", 0) > MAX_FETCH_BYTES:
            raise MailError(f"message {uid} is too large to read ({meta[b'RFC822.SIZE']} bytes)")
        raw = c.fetch([uid], ["BODY.PEEK[]"])[uid][b"BODY[]"]
        return email.message_from_bytes(raw, policy=email.policy.default)

    def read(self, folder: str, uid: int, max_chars: int = 30000) -> dict:
        msg = self._message(folder, uid)
        body = msg.get_body(preferencelist=("plain", "html"))
        text = part_text(body) if body is not None else ""
        out = {"folder": folder, "uid": uid}
        for h in ("From", "To", "Cc", "Reply-To", "Date", "Subject"):
            v = _header(msg, h)
            if v:
                out[h.lower()] = v
        out["attachments"] = [
            {"index": i, "filename": p.get_filename() or "", "type": p.get_content_type(),
             "size": len(p.get_payload(decode=True) or b"")}
            for i, p in enumerate(attachments(msg, body))
        ]
        out["text"] = cut(text, max_chars)
        return out

    def attachment(self, folder: str, uid: int, index: int, max_chars: int = 30000) -> dict:
        msg = self._message(folder, uid)
        parts = attachments(msg, msg.get_body(preferencelist=("plain", "html")))
        if not 0 <= index < len(parts):
            raise MailError(f"message {uid} has no attachment with index {index}")
        part = parts[index]
        ctype = part.get_content_type()
        if ctype.startswith("text/"):
            text = part_text(part)
        elif ctype == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            if inner is None:
                raise MailError("the attached message cannot be read")
            ib = inner.get_body(preferencelist=("plain", "html"))
            heads = "\n".join(f"{h}: {_header(inner, h)}" for h in ("From", "To", "Date", "Subject") if _header(inner, h))
            text = heads + "\n\n" + (part_text(ib) if ib is not None else "")
        elif ctype == "application/pdf":
            text = _pdf_text(part.get_payload(decode=True) or b"")
        else:
            raise MailError(f"attachments of type {ctype} cannot be read")
        return {"folder": folder, "uid": uid, "index": index, "filename": part.get_filename() or "",
                "type": ctype, "text": cut(text, max_chars)}

    # -- changes ---------------------------------------------------------
    def move(self, folder: str, uid: int, to_folder: str) -> str:
        c = self._client()
        if not c.folder_exists(to_folder):
            raise MailError(f'folder "{to_folder}" does not exist')
        c = self._select(folder, readonly=False)
        self._check_uid(c, folder, uid)
        c.move([uid], to_folder)
        return f'OK: message {uid} moved from "{folder}" to "{to_folder}"'

    def copy(self, folder: str, uid: int, to_folder: str) -> str:
        c = self._client()
        if not c.folder_exists(to_folder):
            raise MailError(f'folder "{to_folder}" does not exist')
        c = self._select(folder)
        self._check_uid(c, folder, uid)
        c.copy([uid], to_folder)
        return f'OK: message {uid} copied from "{folder}" to "{to_folder}"'

    def create_folder(self, name: str) -> str:
        c = self._client()
        if c.folder_exists(name):
            raise MailError(f'folder "{name}" exists already')
        c.create_folder(name)
        try:
            c.subscribe_folder(name)
        except IMAPClientError:
            pass
        return f'OK: folder "{name}" created'


# -- message helpers (also used by the tests) --------------------------------

def _date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise MailError(f"invalid date {s!r}, use YYYY-MM-DD") from e


def _decode(b) -> str:
    if b is None:
        return ""
    s = b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)
    try:
        return str(make_header(decode_header(s)))
    except Exception:  # noqa: BLE001 - broken encoded words: keep the raw text
        return s


def _addrs(addrs) -> str:
    out = []
    for a in addrs or ():
        if not a.mailbox:
            continue
        addr = _decode(a.mailbox) + ("@" + _decode(a.host) if a.host else "")
        name = _decode(a.name)
        out.append(f"{name} <{addr}>" if name else addr)
    return ", ".join(out)


def _summary(folder: str, uid: int, d: dict) -> dict:
    env = d.get(b"ENVELOPE")
    flags = {f.decode() if isinstance(f, bytes) else str(f) for f in d.get(b"FLAGS", ())}
    idate = d.get(b"INTERNALDATE")
    return {
        "folder": folder, "uid": uid,
        "date": idate.strftime("%Y-%m-%d %H:%M") if idate else "",
        "from": _addrs(env.from_) if env else "",
        "to": _addrs(env.to) if env else "",
        "subject": _decode(env.subject) if env else "",
        "seen": "\\Seen" in flags, "flagged": "\\Flagged" in flags,
        "size": d.get(b"RFC822.SIZE", 0),
    }


def _header(msg, name: str) -> str:
    try:
        v = msg.get(name)
        return str(v) if v is not None else ""
    except Exception:  # noqa: BLE001 - a malformed header must not stop the read
        return ""


def attachments(msg, body) -> list:
    """The parts that are not the main text: files, inline images, other formats."""
    out = []
    for part in msg.walk():
        if part.is_multipart() or part is body:
            continue
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment" or part.get_filename() or not ctype.startswith("text/"):
            out.append(part)
    return out


def part_text(part) -> str:
    try:
        content = part.get_content()
    except (LookupError, ValueError, AssertionError, KeyError):
        content = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return content.strip()


def cut(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n[... {len(text) - n} more characters not shown]"


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise MailError("PDF attachments cannot be read: pypdf is not installed") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:  # noqa: BLE001 - pypdf raises many types
        raise MailError(f"the PDF cannot be read: {e}") from e


class _HtmlText(HTMLParser):
    SKIP = {"script", "style", "head"}
    BLOCK = {"p", "div", "br", "tr", "li", "ul", "ol", "table", "blockquote", "hr",
             "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
            if tag == "li":
                self.out.append("- ")
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _HtmlText()
    p.feed(html)
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
