"""Job wrapper: run a command, capture output to a log, write a heartbeat,
alert immediately on failure, optionally forward the output as the message."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .cronexpr import parse
from .heartbeat import now_local, write_heartbeat
from .notify import notify

LOG_MAX = 1_000_000


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
    if only_if_cron and not parse(only_if_cron).matches(start.replace(tzinfo=None)):
        return 0                       # superset fire from a gated schedule: not a real slot
    timeout_s = 60 * (timeout_min or cfg.get("job_timeout_minutes", 60))
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
