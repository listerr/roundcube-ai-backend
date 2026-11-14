# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Rob Lister
"""roundcube-ai-admin: enable AI for domains and mailboxes, and test the connections.

  roundcube-ai-admin domain list
  roundcube-ai-admin domain enable|disable|default DOMAIN
  roundcube-ai-admin mailbox list
  roundcube-ai-admin mailbox enable|disable|default LOGIN

"default" removes the setting, so the [access] default of the config applies.
  roundcube-ai-admin check-imap LOGIN
  roundcube-ai-admin check-openai
"""

from __future__ import annotations

import argparse
import sys

from .auth import address_for, ai_default, domain_ai, mailbox_ai
from .config import ConfigError, load
from .db import Database, domain_of


def _table(rows: list[list[str]]) -> None:
    if not rows:
        return
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)).rstrip())


def _yn(v: bool) -> str:
    return "yes" if v else "no"


def cmd_domain(db: Database, cfg, args) -> None:
    acc = cfg.access
    if args.action == "list":
        rows = [["DOMAIN", "AI", "SET BY"]]
        stored = {d["name"]: bool(d["enabled"]) for d in db.domain_list()}
        for name in sorted(set(stored) | set(acc.enable_domains)):
            if name in stored:
                rows.append([name, _yn(stored[name]), "admin"])
            else:
                rows.append([name, _yn(ai_default(acc, name)), "config"])
        _table(rows)
        if acc.enable_all:
            print("other domains: AI yes (config: [access] enable_all)")
        return
    name = args.name.lower()
    if args.action == "default":
        db.domain_delete(name)
        print(f"domain {name}: AI {'enabled' if ai_default(acc, name) else 'disabled'} (config default)")
        return
    db.domain_set(name, args.action == "enable")
    print(f"domain {name}: AI {'enabled' if args.action == 'enable' else 'disabled'}")


def cmd_mailbox(db: Database, cfg, args) -> None:
    acc = cfg.access
    if args.action == "list":
        rows = [["LOGIN", "ADDRESS", "AI", "SET BY", "DOMAIN AI"]]
        for m in db.mailbox_list():
            rows.append([m["login"], m["address"], _yn(mailbox_ai(acc, m)),
                         "config" if m["ai_enable"] is None else "admin",
                         _yn(domain_ai(db, acc, domain_of(m["address"])))])
        _table(rows)
        return
    box = db.mailbox_get_or_create(args.login, address_for(args.login, cfg.auth.default_domain))
    value = None if args.action == "default" else int(args.action == "enable")
    db.mailbox_update(box["id"], {"ai_enable": value})
    box = db.mailbox_by_id(box["id"])
    on = mailbox_ai(acc, box)
    print(f"mailbox {box['login']}: AI {'enabled' if on else 'disabled'}"
          + (" (config default)" if value is None else ""))
    dom = domain_of(box["address"])
    if on and not domain_ai(db, acc, dom):
        print(f"note: the domain {dom or '(none)'} is not enabled. "
              f"Run: roundcube-ai-admin domain enable {dom or 'DOMAIN'}")


def cmd_check_imap(cfg, args) -> int:
    from .mail import ImapBackend, MailUnavailable
    s = ImapBackend(cfg.imap).session(args.login)
    try:
        folders = s.folders()
        limit, used, msgs = s.quota()
    except MailUnavailable as e:
        print(f"FAILED: {e}")
        return 1
    finally:
        s.close()
    print(f"OK: master login as {args.login}{cfg.imap.master_separator}{cfg.imap.master_user}")
    print(f"folders: {len(folders)} ({', '.join(f['name'] for f in folders[:8])}{', ...' if len(folders) > 8 else ''})")
    print(f"quota: used {used} of {limit} bytes, {msgs} messages")
    return 0


def cmd_check_openai(cfg) -> int:
    from openai import OpenAI, OpenAIError
    if not cfg.openai.model or not cfg.openai.api_key:
        print("FAILED: [openai] model or api_key is not set")
        return 1
    client = OpenAI(api_key=cfg.openai.api_key, base_url=cfg.openai.base_url, timeout=60)
    try:
        r = client.chat.completions.create(model=cfg.openai.model, messages=[{"role": "user", "content": "Reply with OK."}],
                                           **cfg.openai.params)
    except OpenAIError as e:
        print(f"FAILED: {e}")
        return 1
    u = r.usage
    print(f"OK: {cfg.openai.model} answered {r.choices[0].message.content!r}"
          + (f" ({u.prompt_tokens} tokens in, {u.completion_tokens} out)" if u else ""))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="roundcube-ai-admin", description="Administration of roundcube-ai-backend.")
    ap.add_argument("-c", "--config", help="config file (default: $RCAI_CONFIG or /etc/roundcube-ai-backend/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("domain", help="enable or disable AI for a domain")
    d.add_argument("action", choices=["list", "enable", "disable", "default"])
    d.add_argument("name", nargs="?")
    m = sub.add_parser("mailbox", help="enable or disable AI for a mailbox")
    m.add_argument("action", choices=["list", "enable", "disable", "default"])
    m.add_argument("login", nargs="?")
    c = sub.add_parser("check-imap", help="test the master-user IMAP login for a mailbox")
    c.add_argument("login")
    sub.add_parser("check-openai", help="send one small request to the model")
    args = ap.parse_args()

    try:
        cfg = load(args.config)
    except ConfigError as e:
        sys.exit(f"roundcube-ai-admin: {e}")

    if args.cmd == "check-imap":
        sys.exit(cmd_check_imap(cfg, args))
    if args.cmd == "check-openai":
        sys.exit(cmd_check_openai(cfg))

    if args.action != "list" and not (args.name if args.cmd == "domain" else args.login):
        ap.error(f"{args.cmd} {args.action} needs a {'domain' if args.cmd == 'domain' else 'login'}")
    db = Database(cfg.database.path)
    try:
        if args.cmd == "domain":
            cmd_domain(db, cfg, args)
        else:
            cmd_mailbox(db, cfg, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
