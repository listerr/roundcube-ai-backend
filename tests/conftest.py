import copy
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rcai.app import create_app
from rcai.config import from_dict
from rcai.mail import MailError

BASE = "/rcapi"


class FakeSession:
    def __init__(self, backend, login):
        self.backend, self.login = backend, login

    def _log(self, *call):
        self.backend.calls.append((self.login, *call))

    def folders(self, counts=False):
        return [{"name": "INBOX"}, {"name": "Junk", "special": "Junk"}]

    def quota(self):
        return 1024 * 1024, 2048, 3

    def search(self, **kw):
        self._log("search", kw)
        return {"total": 1, "shown": 1, "messages": [{"folder": "INBOX", "uid": 7, "subject": "Hello"}]}

    def read(self, folder, uid, max_chars):
        self._log("read", folder, uid)
        if uid != 7:
            raise MailError(f'message {uid} does not exist in folder "{folder}"')
        return {"folder": folder, "uid": uid, "subject": "Hello", "text": "Hi there"}

    def close(self):
        pass


class FakeMail:
    def __init__(self):
        self.calls = []

    def check_login(self, login, password):
        return password == "secret"

    def session(self, login):
        return FakeSession(self, login)


def reply(content=None, calls=()):
    tool_calls = [SimpleNamespace(id=cid, type="function",
                                  function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                  for cid, name, args in calls]
    msg = SimpleNamespace(content=content, tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)],
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


class FakeLLM:
    def __init__(self):
        self.script = []
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        return self.script.pop(0)


@pytest.fixture
def env(tmp_path):
    cfg = from_dict({
        "server": {"cors_origins": ["https://webmail.example.org"]},
        "database": {"path": str(tmp_path / "t.db")},
        "imap": {"master_user": "m", "master_password": "p"},
        "openai": {"api_key": "x", "model": "test-model"},
        "agent": {"ping_interval": 0.05, "max_steps": 4},
    })
    mail, llm = FakeMail(), FakeLLM()
    app = create_app(cfg, mail=mail, llm=llm)
    client = TestClient(app)
    return SimpleNamespace(cfg=cfg, mail=mail, llm=llm, app=app, api=app.app, db=app.app.state.db, client=client)


def login(env, user="rob@example.org", password="secret"):
    r = env.client.post(f"{BASE}/?fn=do_login", data={"login": user, "password": password, "persist": "0"})
    return r


@pytest.fixture
def token(env):
    r = login(env)
    assert r.status_code == 200, r.text
    return r.json()["session"]


def enable(env, user="rob@example.org"):
    env.db.domain_set(user.split("@")[1], True)
    box = env.db.mailbox_get(user)
    env.db.mailbox_update(box["id"], {"ai_enable": 1})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def sse_events(text):
    """data: payloads of an SSE body, as parsed JSON (or the string [DONE])."""
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            p = line[6:]
            out.append(p if p == "[DONE]" else json.loads(p))
    return out


def answer_text(events):
    return "".join(e["choices"][0]["delta"].get("content", "") for e in events
                   if isinstance(e, dict) and "choices" in e)
