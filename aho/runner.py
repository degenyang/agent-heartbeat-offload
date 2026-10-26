"""Job wrapper: run a command, capture output to a log, write a heartbeat,
alert immediately on failure, optionally forward the output as the message."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import as_number
from .cronexpr import parse
from .heartbeat import _naive_local, hb_dir, now_local, write_heartbeat
from .notify import notify

LOG_MAX = 1_000_000


def _skip_superset_fire(state_dir: str, job: str, cron_expr, start, grace_min: int,
                        catchup_min: int = 120) -> bool:
    """A *gated* schedule fires on a superset of its cron times; the runner is given
    ``--only-if-cron`` to drop the extra fires. Return True to drop THIS fire.

    A fire is real when it is the first one at or after a scheduled slot: find the latest
    slot <= now and run unless this job's last heartbeat already covers it. Matching the
    slot (not the exact minute) keeps a legitimately late / catch-up fire -- e.g. the
    machine was asleep at 09:00 and Task Scheduler fires at 09:07 with StartWhenAvailable --
    which an exact ``cron.matches(now)`` check would silently discard, while the heartbeat
    check still drops the genuine superset extras (their slot already ran).

    With a usable heartbeat we can prove whether the slot already ran, so even a long
    catch-up is honoured. WITHOUT one (first run, or the heartbeat was rotated/corrupt) we
    cannot prove a slot was missed, so we run only when the fire is within ``catchup_min``
    of the slot. That window is wider than ``grace_min`` on purpose: a gated schedule with
    one sparse slot/day whose catch-up fire lands a little late must still be able to run its
    FIRST time -- otherwise it never writes a heartbeat, so every later fire is judged with no
    history too and the job is silenced forever. The window stays far below a day, so a true
    superset extra (its real slot is a day or more back) is still dropped."""
    now_n = start.replace(tzinfo=None)
    slot = parse(cron_expr).prev_fire(now_n)
    if slot is None:
        return True                      # this expression never fires: always a superset extra
    try:
        rec = json.loads((hb_dir(state_dir) / f"{job}.json").read_text(encoding="utf-8"))
        last = rec.get("started_at") if isinstance(rec, dict) else None   # valid JSON may not be an object
    except (OSError, ValueError):
        last = None
    if last:
        try:
            return _naive_local(last) >= slot    # slot already ran -> drop; not yet -> run (catch-up)
        except (ValueError, TypeError):
            pass                         # unparseable timestamp: treat as no usable history
    return now_n - slot > dt.timedelta(minutes=max(catchup_min, grace_min))   # no history: bootstrap window


def _log(state_dir: str, job: str, text: str) -> None:
    d = Path(state_dir) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{job}.log"
    if p.exists() and p.stat().st_size > LOG_MAX:
        os.replace(p, p.with_suffix(".log.1"))
    with p.open("a", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def run_job(cfg: dict, job: str, argv: list[str] | None = None, shell: str | None = None,
            timeout_min: float | None = None, only_if_cron: str | None = None,
            notify_output: bool = False, quiet_regex: str | None = None) -> int:
    state = cfg["state_dir"]
    start = now_local()
    if only_if_cron and _skip_superset_fire(state, job, only_if_cron, start,
                                            int(as_number(cfg.get("grace_minutes"), 15)),
                                            int(as_number(cfg.get("catchup_minutes"), 120))):
        return 0                       # superset fire from a gated schedule: not a real slot
    raw_tmo = timeout_min if timeout_min is not None else cfg.get("job_timeout_minutes", 60)
    tmo = as_number(raw_tmo, 60)
    timeout_s = 60 * tmo if tmo > 0 else None   # explicit 0 / negative => no timeout
    write_heartbeat(state, job, started_at=start.isoformat(), finished_at=None,
                    exit_code=None, status="running", duration_s=0, summary="")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW: no console flash
    env = dict(os.environ, AHO_JOB=job, PYTHONIOENCODING="utf-8")
    t0 = time.monotonic()
    try:
        r = subprocess.run(shell if shell is not None else argv, shell=shell is not None,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s, env=env, **kw)
        code, out, err, status = r.returncode, r.stdout or "", r.stderr or "", None
    except subprocess.TimeoutExpired as e:
        code, status = 124, "timeout"
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = f"timed out after {timeout_s}s"
    except OSError as e:
        code, out, err, status = 127, "", f"could not start: {e}", None
    dur = round(time.monotonic() - t0, 1)
    status = status or ("ok" if code == 0 else "failed")
    lines = [ln for ln in (out + "\n" + err).splitlines() if ln.strip()]
    summary = (lines[-1] if lines else "")[:200]
    err_block = ("--- stderr\n" + err) if err else ""
    _log(state, job, f"=== {start.isoformat()} exit={code} {dur}s\n{out}\n{err_block}")
    write_heartbeat(state, job, started_at=start.isoformat(), finished_at=now_local().isoformat(),
                    exit_code=code, status=status, duration_s=dur, summary=summary)
    if code != 0 and cfg.get("alert_on_failure", True):
        notify(cfg, f"[aho] job '{job}' FAILED (exit {code}, {dur}s)\n{summary}")
    elif code == 0 and notify_output and out.strip():
        if not (quiet_regex and re.search(quiet_regex, out.strip())):
            notify(cfg, out.strip())
    return code


def stamp(cfg: dict, job: str, exit_code: int = 0, summary: str = "") -> None:
    """For jobs that stay on the agent: the agent calls ``aho stamp <job>`` at the
    end of its run so the same health check also covers agent-run jobs."""
    t = now_local().isoformat()
    write_heartbeat(cfg["state_dir"], job, started_at=t, finished_at=t, exit_code=exit_code,
                    status="ok" if exit_code == 0 else "failed", duration_s=0, summary=summary[:200])


def main_guard() -> None:
    """pythonw.exe has sys.stdout = None; make prints harmless."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
