from conftest import BASE, answer_text, auth, enable, reply, sse_events

CLIENT_TOOLS = [{"type": "function", "function": {
    "name": "mail_open_gui", "description": "Opens a message.",
    "parameters": {"type": "object", "properties": {"folder": {"type": "string"}, "uid": {"type": "integer"}},
                   "required": ["folder", "uid"]},
}}]


def post(env, token, messages, conv=None, chat_type="live", tools=CLIENT_TOOLS):
    h = {**auth(token), "X-Chat-Type": chat_type}
    if conv:
        h["X-Conversation-Id"] = conv
    return env.client.post(f"{BASE}/v1/chat/completions", headers=h,
                           json={"model": "mail-ai", "stream": True, "messages": messages, "tools": tools})


def test_chat_needs_ai_enabled(env, token):
    r = post(env, token, [{"role": "user", "content": "hi"}])
    assert r.status_code == 403
    assert r.json()["error"]["message"]


def test_chat_needs_token(env):
    r = env.client.post(f"{BASE}/v1/chat/completions", json={"messages": []})
    assert r.status_code == 401


def test_answer_after_server_tools(env, token):
    enable(env)
    env.llm.script = [
        reply(None, [("c1", "mail_search", {"from": "alice"})]),
        reply(None, [("c2", "mail_read", {"folder": "INBOX", "uid": 7})]),
        reply("Alice says **hi**."),
    ]
    r = post(env, token, [{"role": "user", "content": "What did Alice write?"}])
    assert r.status_code == 200
    conv = r.headers["x-conversation-id"]
    events = sse_events(r.text)
    assert events[-1] == "[DONE]"
    assert answer_text(events) == "Alice says **hi**."
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[1]["conversation_id"] == conv

    # the model saw the server tools and the client tools, and the tool results
    req = env.llm.requests[-1]
    names = {t["function"]["name"] for t in req["tools"]}
    assert {"mail_search", "mail_read", "mail_open_gui"} <= names
    assert req["messages"][0]["role"] == "system"
    assert [m["role"] for m in req["messages"][1:]] == ["user", "assistant", "tool", "assistant", "tool"]
    assert '"Hi there"' in req["messages"][-1]["content"]
    assert ("rob@example.org", "search", {
        "folder": None, "from_": "alice", "to": "", "subject": "", "text": "", "since": "", "before": "",
        "unseen": False, "flagged": False, "limit": 20}) in env.mail.calls

    task = env.db.task_get(int(conv), env.db.mailbox_get("rob@example.org")["id"])
    assert task["state"] == "done" and task["tokens_in"] == 30 and task["tokens_out"] == 15
    assert task["title"] == "What did Alice write?"

    # the chat window sees the question and the answer, not the internal steps
    c = env.client.get(f"{BASE}/?fn=ai_get_conversation&at_id={conv}", headers=auth(token)).json()["conversation"]
    assert c["type"] == "live"
    assert c["turns"] == [{"role": "user", "content": "What did Alice write?"},
                          {"role": "assistant", "content": "Alice says **hi**."}]


def test_client_tool_round_trip(env, token):
    enable(env)
    env.llm.script = [reply("Here it is.", [("c1", "mail_open_gui", {"folder": "INBOX", "uid": 7})])]
    first = [{"role": "user", "content": "Show the mail from Alice"}]
    r = post(env, token, first)
    conv = r.headers["x-conversation-id"]
    events = sse_events(r.text)
    assert answer_text(events) == "Here it is."
    calls = [e for e in events if isinstance(e, dict) and e["choices"][0]["delta"].get("tool_calls")]
    tc = calls[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc["index"] == 0 and tc["id"] == "c1" and tc["function"]["name"] == "mail_open_gui"
    assert events[-2]["choices"][0]["finish_reason"] == "tool_calls"

    # the browser ran the tool and sends the result in the next request
    env.llm.script = [reply("The message is open.")]
    msgs = first + [{"role": "assistant", "content": "Here it is.", "tool_calls": [tc]},
                    {"role": "tool", "tool_call_id": "c1", "content": "OK"}]
    r = post(env, token, msgs, conv=conv)
    assert r.status_code == 200
    assert answer_text(sse_events(r.text)) == "The message is open."
    sent = env.llm.requests[-1]["messages"]
    assert sent[-1] == {"role": "tool", "tool_call_id": "c1", "content": "OK"}

    turns = env.client.get(f"{BASE}/?fn=ai_get_conversation&at_id={conv}",
                           headers=auth(token)).json()["conversation"]["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant", "tool", "assistant"]
    assert turns[1]["tool_calls"][0]["function"]["name"] == "mail_open_gui"


def test_unanswered_client_call_gets_error(env, token):
    enable(env)
    env.llm.script = [reply(None, [("c1", "mail_open_gui", {"folder": "INBOX", "uid": 7})])]
    r = post(env, token, [{"role": "user", "content": "Show it"}])
    conv = r.headers["x-conversation-id"]
    env.llm.script = [reply("OK, never mind.")]
    r = post(env, token, [{"role": "user", "content": "Forget it"}], conv=conv)
    assert r.status_code == 200
    sent = env.llm.requests[-1]["messages"]
    assert [m["role"] for m in sent[1:]] == ["user", "assistant", "tool", "user"]
    assert sent[3]["content"].startswith("ERROR")


def test_unknown_tool_and_bad_arguments(env, token):
    enable(env)
    env.llm.script = [
        reply(None, [("c1", "rm_rf", {}), ("c2", "mail_read", {"folder": "INBOX", "uid": 99})]),
        reply("Sorry."),
    ]
    post(env, token, [{"role": "user", "content": "x"}])
    tool_msgs = [m for m in env.llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["content"] == "ERROR: unknown tool rm_rf"
    assert tool_msgs[1]["content"].startswith('ERROR: message 99 does not exist')


def test_last_step_forbids_tools(env, token):
    enable(env)
    env.llm.script = [reply(None, [(f"c{i}", "mail_list_folders", {})]) for i in range(3)] + [reply("Done.")]
    r = post(env, token, [{"role": "user", "content": "x"}])
    assert answer_text(sse_events(r.text)) == "Done."
    assert env.llm.requests[-1]["tool_choice"] == "none"
    assert "tool_choice" not in env.llm.requests[0]


def test_conversation_busy(env, token):
    enable(env)
    env.llm.script = [reply("one")]
    conv = post(env, token, [{"role": "user", "content": "a"}]).headers["x-conversation-id"]
    env.db.task_update(int(conv), state="running")
    r = post(env, token, [{"role": "user", "content": "b"}], conv=conv)
    assert r.status_code == 409


def test_chat_type_cannot_change(env, token):
    enable(env)
    env.llm.script = [reply("one")]
    conv = post(env, token, [{"role": "user", "content": "a"}]).headers["x-conversation-id"]
    r = post(env, token, [{"role": "user", "content": "b"}], conv=conv, chat_type="reply")
    assert r.status_code == 400


def test_spellcheck_has_no_server_tools(env, token):
    enable(env)
    tools = [{"type": "function", "function": {"name": "mail_content_replace", "parameters": {"type": "object"}}}]
    env.llm.script = [reply("Nothing to correct.")]
    r = post(env, token, [{"role": "user", "content": "Helo"}], chat_type="spellcheck", tools=tools)
    assert r.status_code == 200
    req = env.llm.requests[-1]
    assert [t["function"]["name"] for t in req["tools"]] == ["mail_content_replace"]
    assert "compose editor" in req["messages"][0]["content"]


def test_reserved_client_tool_name(env, token):
    enable(env)
    tools = [{"type": "function", "function": {"name": "mail_read"}}]
    r = post(env, token, [{"role": "user", "content": "x"}], tools=tools)
    assert r.status_code == 400


def test_llm_error_ends_stream(env, token):
    enable(env)
    env.llm.script = []          # pop from an empty list: the agent fails
    r = post(env, token, [{"role": "user", "content": "x"}])
    events = sse_events(r.text)
    assert "error" in events[-1]
    conv = int(r.headers["x-conversation-id"])
    assert env.db.task_get(conv, env.db.mailbox_get("rob@example.org")["id"])["state"] == "error"
    assert env.api.state.active == 0


def test_memory_tool(env, token):
    enable(env)
    env.llm.script = [reply(None, [("c1", "memory_update", {"memory": "Likes short answers."})]), reply("Noted.")]
    post(env, token, [{"role": "user", "content": "Remember: short answers"}])
    assert env.db.mailbox_get("rob@example.org")["memory"] == "Likes short answers."


def test_ping_while_working(env, token):
    import asyncio

    enable(env)
    create = env.llm.create

    async def slow(**kw):
        await asyncio.sleep(0.2)
        return await create(**kw)

    env.llm.chat.completions = type("C", (), {"create": staticmethod(slow)})()
    env.llm.script = [reply("late answer")]
    r = post(env, token, [{"role": "user", "content": "x"}])
    assert r.text.startswith(": ping\n\n")
    assert answer_text(sse_events(r.text)) == "late answer"
