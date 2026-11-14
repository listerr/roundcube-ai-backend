from conftest import BASE, auth, enable, login


def test_health_with_and_without_prefix(env):
    assert env.client.get(f"{BASE}/health").json()["ok"] is True
    assert env.client.get("/health").json()["ok"] is True


def test_unknown_operation(env):
    r = env.client.get(f"{BASE}/?fn=nope")
    assert r.status_code == 400
    assert r.json()["code"] == 400


def test_login_wrong_password(env):
    r = login(env, password="bad")
    assert r.status_code == 401
    assert r.json() == {"error": "Invalid username or password.", "code": 401}


def test_login_rate_limit(env):
    for _ in range(5):
        assert login(env, password="bad").status_code == 401
    assert login(env, password="secret").status_code == 429


def test_login_needs_post(env):
    assert env.client.get(f"{BASE}/?fn=do_login&login=a&password=b").status_code == 400


def test_login_and_logout(env):
    r = login(env, user="Rob@Example.org")
    body = r.json()
    assert body["login"] == "rob@example.org"
    assert body["address"] == "rob@example.org"
    assert body["expires_in"] == env.cfg.auth.session_ttl
    tok = body["session"]
    assert env.client.get(f"{BASE}/?fn=mail_get_mailbox", headers=auth(tok)).status_code == 200
    assert env.client.post(f"{BASE}/?fn=do_logout", headers=auth(tok)).json() == {"ok": True}
    assert env.client.get(f"{BASE}/?fn=mail_get_mailbox", headers=auth(tok)).status_code == 401


def test_no_token(env):
    assert env.client.get(f"{BASE}/?fn=mail_get_mailbox").status_code == 401


def test_get_mailbox(env, token):
    box = env.client.get(f"{BASE}/?fn=mail_get_mailbox", headers=auth(token)).json()["mailbox"]
    assert box["address"] == "rob@example.org"
    assert box["quota"] == 1024 * 1024 and box["quota_used_bytes"] == 2048 and box["quota_used_messages"] == 3
    ai = box["ai"]
    assert ai["domain_enabled"] == 0 and ai["enable"] == 0
    assert set(ai["instructions"]) == {"incoming", "summary_daily", "summary_weekly", "reply", "create", "spellcheck"}
    assert ai["instructions"]["reply"]["value"] == ""
    assert ai["instructions"]["reply"]["default"]
    enable(env)
    ai = env.client.get(f"{BASE}/?fn=mail_get_mailbox", headers=auth(token)).json()["mailbox"]["ai"]
    assert ai["domain_enabled"] == 1 and ai["enable"] == 1


def test_store_mailbox(env, token):
    enable(env)
    r = env.client.post(f"{BASE}/?fn=mail_store_mailbox", headers=auth(token), data={
        "fullname": "Rob L", "ai_enable_summary_daily": "1", "ai_reply_instructions": "Be terse.\r\nNo emoji.",
        "ai_memory": "Prefers British English.",
    })
    assert r.status_code == 200, r.text
    box = r.json()["mailbox"]
    assert box["fullname"] == "Rob L"
    assert box["ai"]["enable_summary_daily"] == 1
    assert box["ai"]["instructions"]["reply"]["value"] == "Be terse.\nNo emoji."
    assert box["ai"]["memory"] == "Prefers British English."


def test_store_mailbox_forbidden_field(env, token):
    enable(env)
    r = env.client.post(f"{BASE}/?fn=mail_store_mailbox", headers=auth(token), data={"ai_enable": "1"})
    assert r.status_code == 403


def test_store_mailbox_ai_fields_need_domain(env, token):
    r = env.client.post(f"{BASE}/?fn=mail_store_mailbox", headers=auth(token), data={"ai_memory": "x"})
    assert r.status_code == 403
    r = env.client.post(f"{BASE}/?fn=mail_store_mailbox", headers=auth(token), data={"fullname": "Rob"})
    assert r.status_code == 200


def test_list_tasks_and_usage(env, token):
    box = env.db.mailbox_get("rob@example.org")
    a = env.db.task_create(box["id"], "live", title="first question")
    env.db.turn_add(a, "user", "first question")
    env.db.task_add_usage(a, 100, 20)
    b = env.db.task_create(box["id"], "spellcheck", title="text")
    env.db.turn_add(b, "user", "text")
    env.db.task_add_usage(b, 50, 10)
    env.db.task_create(box["id"], "live", title="no history")      # not in the chat list

    chat = env.client.get(f"{BASE}/?fn=ai_list_tasks&type=chat&limit=50", headers=auth(token)).json()
    assert [t["at_id"] for t in chat["tasks"]] == [a]
    assert chat["tasks"][0]["title"] == "first question"

    log = env.client.get(f"{BASE}/?fn=ai_list_tasks&limit=500&offset=0", headers=auth(token)).json()
    assert log["total"] == 3

    months = env.client.get(f"{BASE}/?fn=ai_usage_months&months=24", headers=auth(token)).json()["months"]
    assert len(months) == 1
    m = months[0]
    assert m["live"] == {"tasks": 3, "tokens_in": 150, "tokens_out": 30}
    assert m["total"]["tasks"] == 3
    assert m["incoming"]["tasks"] == 0

    assert env.client.get(f"{BASE}/?fn=ai_usage_months&months=0", headers=auth(token)).status_code == 400


def test_conversation_of_other_user_is_not_found(env, token):
    other = env.db.mailbox_get_or_create("eve@example.org", "eve@example.org")
    at = env.db.task_create(other["id"], "live")
    r = env.client.get(f"{BASE}/?fn=ai_get_conversation&at_id={at}", headers=auth(token))
    assert r.status_code == 404


def test_cors_preflight(env):
    r = env.client.options(f"{BASE}/v1/chat/completions", headers={
        "Origin": "https://webmail.example.org",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization, content-type, x-conversation-id, x-chat-type",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://webmail.example.org"
    assert "x-chat-type" in r.headers["access-control-allow-headers"].lower()


def test_cors_other_origin_refused(env):
    r = env.client.options(f"{BASE}/v1/chat/completions", headers={
        "Origin": "https://evil.example.net", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
