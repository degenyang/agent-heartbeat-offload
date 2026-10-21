import base64
import json
import urllib.parse

import pytest

from aho import notify as N


class FakeHTTP:
    def __init__(self, status=200, body=b'{"ok": true}'):
        self.calls = []
        self.status, self.body = status, body

    def __call__(self, req, timeout=30):
        self.calls.append(req)
        return self.status, self.body


@pytest.fixture
def fake(monkeypatch):
    f = FakeHTTP()
    monkeypatch.setattr(N, "_http", f)
    monkeypatch.delenv("AHO_DRY_RUN", raising=False)
    return f


TG = {"notify": {"channel": "telegram", "telegram_bot_token": "123:ABC", "telegram_chat_id": "42"}}


def test_telegram_send(fake):
    assert N.notify(TG, "hello") is True
    req = fake.calls[0]
    assert req.full_url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert req.get_method() == "POST"
    form = urllib.parse.parse_qs(req.data.decode())
    assert form["chat_id"] == ["42"] and form["text"] == ["hello"]


def test_telegram_long_message_is_chunked(fake):
    text = "\n".join("x" * 100 for _ in range(100))      # ~10k chars
    assert N.notify(TG, text)
    assert len(fake.calls) == 3
    assert all(len(urllib.parse.parse_qs(c.data.decode())["text"][0]) <= 4000 for c in fake.calls)


def test_telegram_api_error_returns_false(monkeypatch):
    monkeypatch.setattr(N, "_http", FakeHTTP(400, b'{"ok": false}'))
    assert N.notify(TG, "x") is False


def test_telegram_network_error_never_raises(monkeypatch):
    def boom(req, timeout=30):
        raise OSError("network down")
    monkeypatch.setattr(N, "_http", boom)
    assert N.notify(TG, "x") is False


def test_missing_config_returns_false(fake):
    assert N.notify({"notify": {"channel": "telegram"}}, "x") is False
    assert not fake.calls


def test_dry_run_forces_stdout(fake, monkeypatch, capsys):
    monkeypatch.setenv("AHO_DRY_RUN", "1")
    assert N.notify(TG, "dry")
    assert not fake.calls
    assert "dry" in capsys.readouterr().out


def test_twilio_whatsapp(fake):
    cfg = {"notify": {"channel": "whatsapp_twilio", "twilio_account_sid": "AC1", "twilio_auth_token": "t",
                      "twilio_from": "+15550000001", "twilio_to": "whatsapp:+15550000002"}}
    fake.status = 201
    assert N.notify(cfg, "hi")
    req = fake.calls[0]
    assert req.full_url == "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json"
    form = urllib.parse.parse_qs(req.data.decode())
    assert form["From"] == ["whatsapp:+15550000001"] and form["To"] == ["whatsapp:+15550000002"]
    assert form["Body"] == ["hi"]
    assert req.get_header("Authorization") == "Basic " + base64.b64encode(b"AC1:t").decode()


def test_twilio_template(fake):
    cfg = {"notify": {"channel": "whatsapp_twilio", "twilio_account_sid": "AC1", "twilio_auth_token": "t",
                      "twilio_from": "+1", "twilio_to": "+2", "twilio_content_sid": "HX1"}}
    N.notify(cfg, "hi")
    form = urllib.parse.parse_qs(fake.calls[0].data.decode())
    assert form["ContentSid"] == ["HX1"] and json.loads(form["ContentVariables"][0]) == {"1": "hi"}
    assert "Body" not in form


def test_callmebot(fake):
    cfg = {"notify": {"channel": "whatsapp_callmebot", "callmebot_phone": "+15550000003", "callmebot_apikey": "k"}}
    assert N.notify(cfg, "a b")
    u = urllib.parse.urlparse(fake.calls[0].full_url)
    assert u.netloc == "api.callmebot.com" and u.path == "/whatsapp.php"
    q = urllib.parse.parse_qs(u.query)
    assert q == {"phone": ["+15550000003"], "text": ["a b"], "apikey": ["k"]}


def test_webhook(fake):
    assert N.notify({"notify": {"channel": "webhook", "webhook_url": "https://hook.example/x"}}, "yo")
    assert json.loads(fake.calls[0].data) == {"text": "yo"}


def test_unknown_channel():
    with pytest.raises(N.NotifyError):
        N.make_notifier({"notify": {"channel": "carrier-pigeon"}})
