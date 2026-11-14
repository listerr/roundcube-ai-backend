import asyncio

import pytest

from conftest import BASE, auth, enable, login, reply
from rcai.auth import LoginLimiter
from rcai.config import ConfigError, from_dict
from test_chat import CLIENT_TOOLS, post

BASE_CFG = {"imap": {"master_user": "m", "master_password": "p"}, "openai": {"api_key": "x", "model": "m"}}


def cfg(**sections):
    data = {k: dict(v) for k, v in BASE_CFG.items()}
    for name, values in sections.items():
        data.setdefault(name, {}).update(values)
    return from_dict(data)


# -- configuration -------------------------------------------------------

@pytest.mark.parametrize("section,key,value", [
    ("auth", "session_ttl", 0), ("auth", "max_failures", -1), ("agent", "max_concurrent", 0),
    ("agent", "max_steps", 10_000), ("openai", "timeout", 0), ("server", "max_body_bytes", 1),
    ("agent", "max_steps", "5"), ("auth", "max_failures", True),
])
def test_config_ranges(section, key, value):
    with pytest.raises(ConfigError, match=key):
        cfg(**{section: {key: value}})


@pytest.mark.parametrize("section,values", [
    ("imap", {"host": "mail.example.org", "security": "none"}),
    ("imap", {"host": "mail.example.org", "tls_verify": False}),
    ("openai", {"base_url": "http://llm.example.org/v1"}),
])
def test_insecure_transport_refused(section, values):
    with pytest.raises(ConfigError, match="allow_insecure_transport"):
        cfg(**{section: values})
    cfg(**{section: values, "server": {"allow_insecure_transport": True}})


@pytest.mark.parametrize("section,values", [
    ("imap", {"host": "127.0.0.1", "security": "none"}),
    ("imap", {"host": "localhost", "tls_verify": False}),
    ("openai", {"base_url": "http://[::1]:11434/v1"}),
])
def test_insecure_transport_to_loopback_allowed(section, values):
    cfg(**{section: values})


def test_cors_default_is_same_origin():
    assert cfg().server.cors_origins == []


# -- login limits --------------------------------------------------------

def test_limiter_is_per_login_and_address():
    lim = LoginLimiter(max_failures=2, max_failures_ip=100, window=60)
    for _ in range(2):
        lim.fail("Rob", "192.0.2.1")
    assert lim.blocked("rob", "192.0.2.1")
    assert not lim.blocked("rob", "192.0.2.2")      # no lockout from other addresses


def test_limiter_per_address():
    lim = LoginLimiter(max_failures=100, max_failures_ip=3, window=60)
    for i in range(3):
        lim.fail(f"user{i}", "192.0.2.1")
    assert lim.blocked("someone-new", "192.0.2.1")
    assert not lim.blocked("someone-new", "192.0.2.2")


def test_limiter_forgets_expired_entries(monkeypatch):
    lim = LoginLimiter(max_failures=5, max_failures_ip=5, window=10)
    t = [1000.0]
    monkeypatch.setattr("rcai.auth.time.monotonic", lambda: t[0])
    lim._swept = t[0]
    for i in range(50):
        lim.fail(f"user{i}", f"192.0.2.{i}")
    assert len(lim) == 100
    t[0] += 100
    lim.fail("last", "198.51.100.1")                # the sweep runs here
    assert len(lim) == 2


def test_limiter_is_bounded(monkeypatch):
    monkeypatch.setattr(LoginLimiter, "MAX_KEYS", 10)
    lim = LoginLimiter(max_failures=5, max_failures_ip=5, window=600)
    for i in range(100):
        lim.fail(f"user{i}", "192.0.2.1")
    assert len(lim) <= 11


def test_login_concurrency_limit(env):
    env.api.state.logins_active = env.cfg.auth.max_concurrent_logins
    assert login(env).status_code == 429


# -- request size --------------------------------------------------------

def test_body_too_large(env, token):
    big = "x" * (env.cfg.server.max_body_bytes + 1)
    r = env.client.post(f"{BASE}/v1/chat/completions", headers=auth(token),
                        content=big)
    assert r.status_code == 413
    r = env.client.post(f"{BASE}/?fn=do_login", content=big,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_too_many_tools(env, token):
    enable(env)
    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(env.cfg.agent.max_tools + 1)]
    r = post(env, token, [{"role": "user", "content": "hi"}], tools=tools)
    assert r.status_code == 400 and "too many tools" in r.json()["error"]["message"]


def test_tool_definitions_too_large(env, token):
    enable(env)
    tools = [{"type": "function", "function": {"name": "t", "description": "x" * env.cfg.agent.max_tool_bytes}}]
    r = post(env, token, [{"role": "user", "content": "hi"}], tools=tools)
    assert r.status_code == 400 and "too large" in r.json()["error"]["message"]


def test_bad_tool_name(env, token):
    enable(env)
    tools = [{"type": "function", "function": {"name": "a b"}}]
    assert post(env, token, [{"role": "user", "content": "hi"}], tools=tools).status_code == 400


# -- chat limits ---------------------------------------------------------

def test_one_run_per_mailbox(env, token):
    enable(env)
    mid = env.db.mailbox_get("rob@example.org")["id"]
    env.api.state.active_by_box[mid] = 1
    r = post(env, token, [{"role": "user", "content": "hi"}])
    assert r.status_code == 429


def test_counters_return_to_zero(env, token):
    enable(env)
    env.llm.script = [reply("ok")]
    assert post(env, token, [{"role": "user", "content": "hi"}]).status_code == 200
    assert env.api.state.active == 0 and env.api.state.active_by_box == {}


def test_daily_token_limit(env, token):
    enable(env)
    env.cfg.agent.daily_tokens = 10
    env.llm.script = [reply("ok")]                  # uses 15 tokens
    assert post(env, token, [{"role": "user", "content": "hi"}]).status_code == 200
    r = post(env, token, [{"role": "user", "content": "again"}])
    assert r.status_code == 429 and "daily" in r.json()["error"]["message"]


def test_run_timeout(env, token):
    enable(env)
    env.cfg.agent.run_timeout = 0.2

    async def slow(**kw):
        await asyncio.sleep(5)

    env.llm.create = slow
    r = post(env, token, [{"role": "user", "content": "hi"}], tools=CLIENT_TOOLS)
    assert "took too long" in r.text
    task = env.db.task_get(int(r.headers["x-conversation-id"]), env.db.mailbox_get("rob@example.org")["id"])
    assert task["state"] == "error"


# -- search limit ----------------------------------------------------------

@pytest.mark.parametrize("configured,asked,expected", [
    (100, None, 20), (100, 500, 100), (100, 50, 50), (0, 5000, 5000), (0, None, 20),
])
def test_search_limit(configured, asked, expected):
    from rcai import tools

    class Mail:
        def search(self, **kw):
            self.kw = kw
            return {}

    mail = Mail()
    ctx = tools.ToolContext(None, 1, mail, configured, 1000)
    args = {} if asked is None else {"limit": asked}
    asyncio.run(tools._dispatch("mail_search", args, ctx))
    assert mail.kw["limit"] == expected


def test_search_limit_zero_is_allowed():
    assert cfg(agent={"search_limit": 0}).agent.search_limit == 0
