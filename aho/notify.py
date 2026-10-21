"""Notification adapters. Each adapter implements ``send(text) -> bool``.

* ``telegram``            -- Telegram Bot API ``sendMessage`` (fully supported).
* ``whatsapp_twilio``     -- Twilio Programmable Messaging, WhatsApp sender.
* ``whatsapp_callmebot``  -- CallMeBot free WhatsApp API (personal use).
* ``webhook``             -- POST ``{"text": ...}`` JSON to any URL (n8n, Make,
                             Zapier, Home Assistant, a WhatsApp gateway, Slack/Discord-compatible relays ...).
* ``stdout``              -- print only (default; also forced by ``AHO_DRY_RUN=1``).

Add your own: subclass :class:`Notifier` and register it in ``ADAPTERS``.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request


def _http(req: urllib.request.Request, timeout: int = 30) -> tuple[int, bytes]:
    """Single HTTP seam (tests monkeypatch this)."""
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


class NotifyError(RuntimeError):
    pass


class Notifier:
    name = "base"
    max_len = 4000

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _need(self, *keys: str) -> list[str]:
        missing = [k for k in keys if not self.cfg.get(k)]
        if missing:
            raise NotifyError(f"{self.name}: missing config {', '.join(missing)}")
        return [str(self.cfg[k]) for k in keys]

    def chunks(self, text: str) -> list[str]:
        out, buf = [], ""
        for line in text.splitlines(keepends=True):
            while len(line) > self.max_len:
                out.append(line[:self.max_len])
                line = line[self.max_len:]
            if len(buf) + len(line) > self.max_len:
                out.append(buf)
                buf = ""
            buf += line
        if buf:
            out.append(buf)
        return out or [""]

    def send(self, text: str) -> bool:
        return all(self.send_one(c) for c in self.chunks(text))

    def send_one(self, text: str) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError


class StdoutNotifier(Notifier):
    name = "stdout"
    max_len = 10**9

    def send_one(self, text: str) -> bool:
        out = sys.stdout or open(os.devnull, "w")
        out.write(f"[aho notify]\n{text}\n")
        return True


class TelegramNotifier(Notifier):
    name = "telegram"
    max_len = 4000            # Telegram limit is 4096 chars per message

    def send_one(self, text: str) -> bool:
        token, chat = self._need("telegram_bot_token", "telegram_chat_id")
        data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=data, method="POST")
        status, body = _http(req)
        try:
            ok = status == 200 and json.loads(body.decode() or "{}").get("ok", False)
        except ValueError:
            ok = False
        return bool(ok)


class TwilioWhatsAppNotifier(Notifier):
    """Twilio WhatsApp. Business-initiated messages outside the 24h customer-service
    window must use an approved template: set ``twilio_content_sid`` and the text is
    passed as template variable ``{"1": text}``."""
    name = "whatsapp_twilio"
    max_len = 1500

    def send_one(self, text: str) -> bool:
        sid, tok, frm, to = self._need("twilio_account_sid", "twilio_auth_token",
                                       "twilio_from", "twilio_to")
        wa = lambda n: n if n.startswith("whatsapp:") else f"whatsapp:{n}"  # noqa: E731
        form = {"From": wa(frm), "To": wa(to)}
        if self.cfg.get("twilio_content_sid"):
            form["ContentSid"] = self.cfg["twilio_content_sid"]
            form["ContentVariables"] = json.dumps({"1": text})
        else:
            form["Body"] = text
        auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=urllib.parse.urlencode(form).encode(), method="POST",
            headers={"Authorization": f"Basic {auth}"})
        status, _ = _http(req)
        return 200 <= status < 300


class CallMeBotWhatsAppNotifier(Notifier):
    name = "whatsapp_callmebot"
    max_len = 1000

    def send_one(self, text: str) -> bool:
        phone, key = self._need("callmebot_phone", "callmebot_apikey")
        q = urllib.parse.urlencode({"phone": phone, "text": text, "apikey": key})
        req = urllib.request.Request(f"https://api.callmebot.com/whatsapp.php?{q}", method="GET")
        status, _ = _http(req)
        return status == 200


class WebhookNotifier(Notifier):
    name = "webhook"
    max_len = 10**6

    def send_one(self, text: str) -> bool:
        (url,) = self._need("webhook_url")
        field = self.cfg.get("webhook_field", "text")
        req = urllib.request.Request(url, data=json.dumps({field: text}).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        status, _ = _http(req)
        return 200 <= status < 300


ADAPTERS = {c.name: c for c in (StdoutNotifier, TelegramNotifier, TwilioWhatsAppNotifier,
                                 CallMeBotWhatsAppNotifier, WebhookNotifier)}


def make_notifier(cfg: dict) -> Notifier:
    ncfg = cfg.get("notify", {}) if "notify" in cfg else cfg
    channel = "stdout" if os.environ.get("AHO_DRY_RUN") else ncfg.get("channel", "stdout")
    cls = ADAPTERS.get(channel)
    if not cls:
        raise NotifyError(f"unknown notify channel {channel!r}; choose one of {sorted(ADAPTERS)}")
    return cls(ncfg)


def notify(cfg: dict, text: str) -> bool:
    """Send, never raise (a broken channel must not crash a job)."""
    try:
        return make_notifier(cfg).send(text)
    except Exception as e:  # noqa: BLE001
        try:
            (sys.stderr or open(os.devnull, "w")).write(f"aho notify failed: {e}\n")
        except Exception:  # noqa: BLE001
            pass
        return False
