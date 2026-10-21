"""Configuration: a JSON file (gitignored) + environment-variable overrides.

Lookup order for the file: ``--config`` > ``$AHO_CONFIG`` > ``./aho.json`` >
``~/.aho/aho.json``. Secrets should come from environment variables or from
that private file (``chmod 600``); never commit them.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS: dict = {
    "state_dir": "~/.aho",              # heartbeats/, logs/, manifest.json, alert_state.json
    "grace_minutes": 15,                # how late a run may be before it counts as missed
    "realert_hours": 12,                # repeat an unchanged alert after this long
    "healthcheck_schedule": "*/30 * * * *",
    "digest_schedule": "0 8 * * *",
    "alert_on_failure": True,           # runner alerts immediately on non-zero exit
    "job_timeout_minutes": 60,
    "timezone": None,                   # optional IANA tz appended to systemd OnCalendar
    "notify": {"channel": "stdout"},    # telegram | whatsapp_twilio | whatsapp_callmebot | webhook | stdout
}

ENV_MAP = {
    "AHO_STATE_DIR": ("state_dir",),
    "AHO_NOTIFY_CHANNEL": ("notify", "channel"),
    "AHO_TELEGRAM_BOT_TOKEN": ("notify", "telegram_bot_token"),
    "AHO_TELEGRAM_CHAT_ID": ("notify", "telegram_chat_id"),
    "AHO_WEBHOOK_URL": ("notify", "webhook_url"),
    "AHO_TWILIO_ACCOUNT_SID": ("notify", "twilio_account_sid"),
    "AHO_TWILIO_AUTH_TOKEN": ("notify", "twilio_auth_token"),
    "AHO_TWILIO_FROM": ("notify", "twilio_from"),
    "AHO_TWILIO_TO": ("notify", "twilio_to"),
    "AHO_TWILIO_CONTENT_SID": ("notify", "twilio_content_sid"),
    "AHO_CALLMEBOT_PHONE": ("notify", "callmebot_phone"),
    "AHO_CALLMEBOT_APIKEY": ("notify", "callmebot_apikey"),
}


def find_config(explicit: str | None = None) -> Path | None:
    for cand in (explicit, os.environ.get("AHO_CONFIG"), "aho.json", "~/.aho/aho.json"):
        if cand:
            p = Path(cand).expanduser()
            if p.is_file():
                return p.resolve()
    return None


def load_config(explicit: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    path = find_config(explicit)
    if explicit and path is None:
        raise FileNotFoundError(f"config not found: {explicit}")
    if path:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        cfg["_config_path"] = str(path)
    for env, keys in ENV_MAP.items():
        val = os.environ.get(env)
        if val:
            tgt = cfg
            for k in keys[:-1]:
                tgt = tgt.setdefault(k, {})
            tgt[keys[-1]] = val
    cfg["state_dir"] = str(Path(cfg["state_dir"]).expanduser())
    return cfg
