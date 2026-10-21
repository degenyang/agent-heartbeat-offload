"""Heartbeat files, staleness evaluation and the report text.

A heartbeat is ``<state_dir>/heartbeats/<job>.json``::

    {"job": "...", "started_at": ISO, "finished_at": ISO|null, "exit_code": int|null,
     "status": "running|ok|failed|timeout", "duration_s": float, "summary": "...", "host": "..."}

Evaluation against the job's cron schedule (local time):
  expected = last scheduled fire time <= now - grace
  NEVER    no heartbeat although a run was expected after registration
  PENDING  no heartbeat and no run expected yet (just registered)
  MISSED   last run started before ``expected`` -> a scheduled run did not happen
  FAILED   last run exited non-zero / timed out
  HUNG     still "running" long after its timeout
  RUNNING  running, within its timeout
  OK       otherwise
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from .cronexpr import parse

BAD = ("NEVER", "MISSED", "FAILED", "HUNG")


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _naive_local(ts: str | dt.datetime) -> dt.datetime:
    t = dt.datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t


def hb_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / "heartbeats"


def write_heartbeat(state_dir: str | Path, job: str, **fields) -> Path:
    d = hb_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"job": job, "host": socket.gethostname(), **fields}
    p = d / f"{job}.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)        # atomic: a reader never sees a half-written file
    return p


def read_heartbeats(state_dir: str | Path) -> dict[str, dict]:
    out = {}
    d = hb_dir(state_dir)
    if d.is_dir():
        for p in d.glob("*.json"):
            try:
                out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                out[p.stem] = {"job": p.stem, "status": "unreadable"}
    return out


@dataclass
class JobStatus:
    job: str
    title: str
    kind: str
    status: str
    detail: str
    last_run: str | None = None

    @property
    def bad(self) -> bool:
        return self.status in BAD


def _ago(delta: dt.timedelta) -> str:
    s = int(delta.total_seconds())
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m ago"
    return f"{s // 86400}d{(s % 86400) // 3600}h ago"


def evaluate_job(job: dict, hb: dict | None, now: dt.datetime, grace_min: int = 15) -> JobStatus:
    name = job["name"]
    title = job.get("title", name)
    kind = job.get("kind", "native")
    now_n = _naive_local(now)
    cron = parse(job["schedule"])
    expected = cron.prev_fire(now_n - dt.timedelta(minutes=grace_min))
    registered = _naive_local(job["registered_at"]) if job.get("registered_at") else None
    timeout = dt.timedelta(minutes=job.get("timeout_minutes", 60))

    if not hb:
        if expected is None or (registered and expected < registered):
            return JobStatus(name, title, kind, "PENDING", "registered, no run due yet")
        return JobStatus(name, title, kind, "NEVER", f"no heartbeat; a run was due {expected:%Y-%m-%d %H:%M}")
    try:
        started = _naive_local(hb["started_at"])
    except (KeyError, ValueError, TypeError):
        return JobStatus(name, title, kind, "FAILED", "heartbeat unreadable")
    last = f"{started:%Y-%m-%d %H:%M}"
    if hb.get("status") == "running":
        if now_n - started > timeout + dt.timedelta(minutes=grace_min):
            return JobStatus(name, title, kind, "HUNG", f"running since {last} (> {timeout})", last)
        return JobStatus(name, title, kind, "RUNNING", f"started {_ago(now_n - started)}", last)
    if expected and started < expected and not (registered and expected < registered):
        return JobStatus(name, title, kind, "MISSED",
                         f"last run {last} ({_ago(now_n - started)}); expected one at {expected:%Y-%m-%d %H:%M}", last)
    code = hb.get("exit_code")
    if hb.get("status") in ("failed", "timeout", "unreadable") or (code not in (0, None)):
        summ = (hb.get("summary") or "").strip()
        return JobStatus(name, title, kind, "FAILED",
                         f"exit {code} at {last}" + (f": {summ[:120]}" if summ else ""), last)
    return JobStatus(name, title, kind, "OK", f"last run {last} ({_ago(now_n - started)})", last)


def evaluate(manifest: dict, heartbeats: dict, now: dt.datetime | None = None,
             grace_min: int = 15) -> list[JobStatus]:
    now = now or now_local()
    return [evaluate_job(j, heartbeats.get(j["name"]), now, j.get("grace_minutes", grace_min))
            for j in manifest.get("jobs", [])]


ICON = {"OK": "OK", "RUNNING": "..", "PENDING": "--", "NEVER": "!!", "MISSED": "!!",
        "FAILED": "XX", "HUNG": "XX"}


def format_report(statuses: list[JobStatus], mode: str = "digest", host: str | None = None) -> str:
    host = host or socket.gethostname()
    bad = [s for s in statuses if s.bad]
    stamp = f"{now_local():%Y-%m-%d %H:%M %Z}".strip()
    if mode == "alert":
        head = f"[aho] ALERT {host} {stamp}: {len(bad)} job(s) need attention"
        rows = bad
    else:
        n_ok = sum(1 for s in statuses if s.status == "OK")
        head = (f"[aho] heartbeat digest {host} {stamp}\n"
                f"{n_ok}/{len(statuses)} OK, {len(bad)} problem(s)")
        rows = sorted(statuses, key=lambda s: (not s.bad, s.kind, s.job))
    lines = [head]
    for s in rows:
        tag = " (agent)" if s.kind == "agent" else ""
        lines.append(f"[{ICON.get(s.status, '??')}] {s.status:<7} {s.title}{tag} - {s.detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------- alert dedupe
def alert_signature(statuses: list[JobStatus]) -> str:
    key = "|".join(sorted(f"{s.job}:{s.status}:{s.last_run}" for s in statuses if s.bad))
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else ""


def should_alert(state_dir: str | Path, sig: str, now: dt.datetime, realert_hours: float) -> bool:
    """True if this set of problems is new, or unchanged for longer than realert_hours."""
    if not sig:
        return False
    p = Path(state_dir) / "alert_state.json"
    try:
        prev = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    if prev.get("sig") == sig:
        last = _naive_local(prev.get("at", "1970-01-01T00:00:00"))
        if _naive_local(now) - last < dt.timedelta(hours=realert_hours):
            return False
    return True


def record_alert(state_dir: str | Path, sig: str, now: dt.datetime) -> None:
    p = Path(state_dir) / "alert_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"sig": sig, "at": now.isoformat()}), encoding="utf-8")
