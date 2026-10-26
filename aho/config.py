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
    "catchup_minutes": 120,             # gated job, no heartbeat yet: run a fire up to this late
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


def as_number(v, default: float) -> float:
    """Coerce a config/manifest value to a number, tolerating a JSON string like "60".
    A bool (JSON true/false) or anything unparseable falls back to ``default`` -- these
    values feed arithmetic (``v > 0``, ``60 * v``), which raises on a str/None/bool."""
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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
    # numeric fields feed arithmetic downstream; a hand-edited aho.json may carry strings.
    cfg["grace_minutes"] = int(as_number(cfg.get("grace_minutes"), 15))
    cfg["catchup_minutes"] = int(as_number(cfg.get("catchup_minutes"), 120))
    cfg["realert_hours"] = as_number(cfg.get("realert_hours"), 12)
    cfg["job_timeout_minutes"] = as_number(cfg.get("job_timeout_minutes"), 60)
    return cfg
