import sqlite3
from types import SimpleNamespace

import pytest

from rcai.auth import ai_allowed, domain_ai, mailbox_ai
from rcai.config import ConfigError, from_dict
from rcai.db import Database


def access(**kw):
    return from_dict({"access": kw}).access


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def box(db, login="rob@example.org"):
    db.mailbox_get_or_create(login, login)
    return db.mailbox_get(login)


def test_off_by_default(db):
    assert not ai_allowed(db, access(), box(db))


def test_enable_all(db):
    assert ai_allowed(db, access(enable_all=True), box(db))


def test_enable_domains(db):
    acc = access(enable_domains=["Example.ORG "])
    assert acc.enable_domains == ["example.org"]
    assert ai_allowed(db, acc, box(db))
    assert not ai_allowed(db, acc, box(db, "ann@other.org"))


def test_admin_setting_overrides_default(db):
    acc = access(enable_all=True)
    b = box(db)
    db.mailbox_update(b["id"], {"ai_enable": 0})
    assert not ai_allowed(db, acc, db.mailbox_get("rob@example.org"))
    db.mailbox_update(b["id"], {"ai_enable": None})
    db.domain_set("example.org", False)
    assert not domain_ai(db, acc, "example.org")
    db.domain_delete("example.org")
    assert ai_allowed(db, acc, db.mailbox_get("rob@example.org"))


def test_admin_enable_without_default(db):
    acc = access()
    b = box(db)
    db.mailbox_update(b["id"], {"ai_enable": 1})
    assert mailbox_ai(acc, db.mailbox_get("rob@example.org"))
    assert not ai_allowed(db, acc, db.mailbox_get("rob@example.org"))
    db.domain_set("example.org", True)
    assert ai_allowed(db, acc, db.mailbox_get("rob@example.org"))


@pytest.mark.parametrize("bad", [{"enable_all": "yes"}, {"enable_domains": "example.org"}, {"enable_domains": [1]}])
def test_bad_config(bad):
    with pytest.raises(ConfigError):
        from_dict({"access": bad})


def test_data_api_shows_default(env, token):
    env.cfg.access.enable_domains = ["example.org"]
    ai = env.client.get("/rcapi/?fn=mail_get_mailbox", headers={"Authorization": f"Bearer {token}"}).json()["mailbox"]["ai"]
    assert ai["domain_enabled"] == 1 and ai["enable"] == 1


def test_migrate_not_null_column(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE mailbox (id INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT NOT NULL UNIQUE,
            address TEXT NOT NULL, ai_enable INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
        CREATE TABLE session (token_hash TEXT PRIMARY KEY,
            mailbox_id INTEGER NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
            created TEXT NOT NULL, expires REAL NOT NULL);
        INSERT INTO mailbox (login, address, ai_enable, created) VALUES ('a@x.org', 'a@x.org', 0, 'now');
        INSERT INTO mailbox (login, address, ai_enable, created) VALUES ('b@x.org', 'b@x.org', 1, 'now');
        INSERT INTO session VALUES ('h', 1, 'now', 1e12);
    """)
    c.close()
    db = Database(path)
    assert db.mailbox_get("a@x.org")["ai_enable"] is None
    assert db.mailbox_get("b@x.org")["ai_enable"] == 1
    assert db.mailbox_get("a@x.org")["memory"] == ""
    assert db._one("SELECT COUNT(*) AS n FROM session")["n"] == 1
    assert db._one("PRAGMA foreign_keys")["foreign_keys"] == 1
    db.close()
    Database(path).close()          # the second open does nothing
