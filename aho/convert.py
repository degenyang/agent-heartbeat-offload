"""Generate native scheduler files (dry run) and optionally register them (--apply)."""
from __future__ import annotations

import json
import math
import os
import platform
import posixpath
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import schedulers as S
from .classify import HYBRID, LLM, LOCAL, Verdict
from .config import as_number
from .heartbeat import now_local
from .specs import Workflow

STUB_MARKER = "AHO_STUB_NOT_IMPLEMENTED"
PKG_ROOT = Path(__file__).resolve().parent.parent
OSES = ("windows", "linux", "macos")
CRON_BEGIN, CRON_END = "# >>> aho (agent-heartbeat-offload) >>>", "# <<< aho (agent-heartbeat-offload) <<<"


def detect_os() -> str:
    s = platform.system().lower()
    return {"windows": "windows", "darwin": "macos"}.get(s, "linux")


@dataclass
class Job:
    slug: str
    title: str
    schedule: str
    kind: str                      # native | agent | builtin
    verdict: str
    shell: str | None = None       # explicit command
    stub: str | None = None        # path of stub runner
    notify_output: bool = False
    quiet_if: str | None = None
    implemented: bool = True
    notes: list = field(default_factory=list)


# ------------------------------------------------------------------- stubs
STUB_TEMPLATE = '''#!/usr/bin/env python3
# {marker}
#   ^ delete this marker line once the logic below really works.
#     `aho convert --apply` refuses to register this job while the marker is present.
#
# aho stub for workflow: {name}
# verdict: {verdict}   schedule: {schedule}
# classifier reason: {reason}
#
# Original description:
{desc}
#
# Commands found verbatim in the spec (verify before relying on them):
{cmds}
#
# Contract: exit 0 on success, non-zero on failure (the runner alerts on failure).
# Anything printed to stdout is forwarded to your channel (job runs with --notify-output);
# print nothing to stay silent. The runner writes the heartbeat for you.
import subprocess
import sys

COMMANDS = {cmd_list!r}


def llm_step(text: str) -> str:
    """HYBRID only: the isolated LLM step (e.g. summarise). Call it ONLY when needed
    (e.g. something changed). Wire it to any CLI you like, for example:
        subprocess.run(["llm", "-m", "<model>"], input=text, capture_output=True, text=True).stdout
    Returning the text unchanged keeps the job fully LLM-free."""
    return text


def main() -> int:
    # TODO: implement the deterministic part of this workflow here.
    print("{name}: stub not implemented yet", file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
'''


def _comment(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()[:limit]
    return "\n".join("#   " + ln for ln in text.splitlines()) or "#   (none)"


def write_stub(path: Path, wf: Workflow, v: Verdict) -> bool:
    """Write a stub unless one exists. Returns True if the (existing) stub is implemented."""
    if path.exists():
        return STUB_MARKER not in path.read_text(encoding="utf-8", errors="replace")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB_TEMPLATE.format(
        marker=STUB_MARKER, name=wf.name.replace("\n", " "), verdict=v.verdict, schedule=wf.schedule,
        reason=v.reason.replace("\n", " ")[:400], desc=_comment(wf.description),
        cmds=_comment("\n".join(v.commands)), cmd_list=v.commands), encoding="utf-8")
    return False


# -------------------------------------------------------------- plan jobs
def plan_jobs(workflows: list[Workflow], verdicts: list[Verdict], out: Path, cfg: dict) -> tuple[list[Job], list[str]]:
    jobs: list[Job] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for wf, v in zip(workflows, verdicts):
        slug = v.slug
        n = 2
        while slug in seen:
            slug = f"{v.slug}_{n}"
            n += 1
        seen.add(slug)
        if not v.schedulable:
            skipped.append(f"{wf.name}: {v.reason}")
            continue
        if v.verdict == LLM:
            jobs.append(Job(slug, wf.name, wf.schedule, "agent", v.verdict,
                            notes=["stays on the agent; add `python -m aho stamp " + slug +
                                   "` as the agent's last step so the health check covers it"]))
            continue
        j = Job(slug, wf.name, wf.schedule, "native", v.verdict,
                notify_output=bool(wf.extra.get("notify_output", False)),
                quiet_if=wf.extra.get("quiet_if"))
        if wf.command and v.verdict == LOCAL:
            j.shell = str(wf.command)
        else:
            stub = (out / "jobs" / f"{slug}.py").resolve()
            j.stub = str(stub)
            j.implemented = write_stub(stub, wf, v)
            j.notify_output = True
            if not j.implemented:
                j.notes.append(f"stub runner {stub.name}: implement it, then remove the {STUB_MARKER} line")
        jobs.append(j)
    jobs.append(Job("aho_healthcheck", "aho health check", cfg.get("healthcheck_schedule", "*/30 * * * *"),
                    "builtin", LOCAL))
    jobs.append(Job("aho_digest", "aho daily digest", cfg.get("digest_schedule", "0 8 * * *"),
                    "builtin", LOCAL))
    return jobs, skipped


# -------------------------------------------------------------- invocation
def python_for(target: str, override: str | None) -> str:
    if override:
        return override
    if target == detect_os():
        exe = Path(sys.executable)
        if target == "windows":
            w = exe.with_name("pythonw.exe")
            return str(w if w.exists() else exe)
        return str(exe)
    return "pythonw.exe" if target == "windows" else "/usr/bin/python3"


def root_for(target: str) -> str:
    return str(PKG_ROOT) if target == detect_os() else "<AHO_ROOT>"


def job_args(j: Job, cfg_path: str | None, gate: bool, py_inner: str, cross: bool = False) -> list[str]:
    """Arguments after `python -m aho`."""
    base = (["--config", cfg_path] if cfg_path else [])
    if j.kind == "builtin":
        mode = "alert" if j.slug == "aho_healthcheck" else "digest"
        return base + ["heartbeat-check", "--mode", mode]
    a = base + ["run", "--job", j.slug]
    if gate:
        a += ["--only-if-cron", j.schedule]
    if j.notify_output:
        a.append("--notify-output")
    if j.quiet_if:
        a += ["--quiet-if", j.quiet_if]
    if j.shell:
        return a + ["--shell", j.shell]
    stub = f"<AHO_OUT>/jobs/{Path(j.stub).name}" if cross else j.stub
    return a + ["--", py_inner, stub]


# -------------------------------------------------------------- emit
def emit(jobs: list[Job], out: Path, target: str, cfg: dict, python: str | None = None,
         cfg_path: str | None = None) -> dict:
    """Write scheduler files for one OS. Returns {slug: {files, notes, gated}}."""
    py = python_for(target, python)
    root = root_for(target)
    cross = target != detect_os()
    # the job's own interpreter: on Windows use python.exe (console-less parent already)
    py_inner = py.replace("pythonw.exe", "python.exe") if target == "windows" else py
    res: dict = {}
    native = [j for j in jobs if j.kind in ("native", "builtin")]
    d = out / target
    d.mkdir(parents=True, exist_ok=True)

    tmo_cfg = as_number(cfg.get("job_timeout_minutes"), 60)
    # round a fractional timeout UP so the OS hard limit is never shorter than the runner's own;
    # <= 0 => no timeout (PT0S in the XML). int() here would turn 0.5 into 0 and silently disable it.
    win_limit = math.ceil(tmo_cfg) + 5 if tmo_cfg > 0 else 0
    if target == "windows":
        reg, unreg = ["@echo off", "rem Generated by aho. Registers tasks under the \\aho\\ folder."], ["@echo off"]
        for j in native:
            plan = S.windows_plan(j.schedule)      # compute the layout (incl. the gated flag) once
            args = job_args(j, cfg_path, plan[2], py_inner, cross)
            em = S.windows_task_xml(j.schedule, py, S.quote_cmd(["-m", "aho"] + args, windows=True), root,
                                    description=f"aho: {j.title} ({j.schedule})",
                                    time_limit_min=win_limit, plan=plan)
            f = d / f"{j.slug}.xml"
            f.write_text(em.text, encoding="utf-16")
            res[j.slug] = {"files": [str(f)], "notes": em.notes, "gated": em.gated}
            line = f'schtasks /Create /TN "aho\\{j.slug}" /XML "{f}" /F'
            reg.append(line if j.implemented else f"rem SKIPPED (stub not implemented): {line}")
            unreg.append(f'schtasks /Delete /TN "aho\\{j.slug}" /F')
        (d / "register.cmd").write_text("\r\n".join(reg) + "\r\n", encoding="utf-8")
        (d / "unregister.cmd").write_text("\r\n".join(unreg) + "\r\n", encoding="utf-8")

    elif target == "linux":
        cron_lines = [CRON_BEGIN, "# Generated by aho. Times are the system time zone."]
        sd = d / "systemd"
        sd.mkdir(exist_ok=True)
        inst = ["#!/bin/sh", "set -e", 'U="$HOME/.config/systemd/user"', 'mkdir -p "$U"']
        uninst = ["#!/bin/sh"]
        for j in native:
            args = job_args(j, cfg_path, False, py, cross)
            cmd = f"cd {shlex.quote(root)} && {S.quote_cmd([py, '-m', 'aho'] + args)} >/dev/null 2>&1"
            line = S.crontab_line(j.schedule, cmd.replace("%", "\\%")).text
            cron_lines.append(line if j.implemented else "# SKIPPED (stub not implemented): " + line)
            svc, tmr, cal = S.systemd_units(j.slug, j.schedule,
                                            S.quote_cmd([py, "-m", "aho"] + args).replace("%", "%%"),
                                            root, j.title, cfg.get("timezone"))
            fs, ft = sd / f"aho-{j.slug}.service", sd / f"aho-{j.slug}.timer"
            fs.write_text(svc, encoding="utf-8")
            ft.write_text(tmr, encoding="utf-8")
            pre = "" if j.implemented else "# SKIPPED (stub not implemented): "
            inst += [f'{pre}cp "$(dirname "$0")/systemd/aho-{j.slug}.service" "$(dirname "$0")/systemd/aho-{j.slug}.timer" "$U/"']
            uninst += [f"systemctl --user disable --now aho-{j.slug}.timer || true",
                       f'rm -f "$HOME/.config/systemd/user/aho-{j.slug}".service "$HOME/.config/systemd/user/aho-{j.slug}".timer']
            res[j.slug] = {"files": [str(fs), str(ft)], "notes": cal.notes, "gated": False,
                           "oncalendar": cal.text}
        inst.append("systemctl --user daemon-reload")
        for j in native:
            if j.implemented:
                inst.append(f"systemctl --user enable --now aho-{j.slug}.timer")
        inst.append("# run timers while logged out:  loginctl enable-linger \"$USER\"")
        uninst.append("systemctl --user daemon-reload")
        cron_lines.append(CRON_END)
        (d / "crontab.txt").write_text("\n".join(cron_lines) + "\n", encoding="utf-8")
        (d / "install-systemd.sh").write_text("\n".join(inst) + "\n", encoding="utf-8", newline="\n")
        (d / "uninstall-systemd.sh").write_text("\n".join(uninst) + "\n", encoding="utf-8", newline="\n")

    elif target == "macos":
        inst = ["#!/bin/sh", 'A="$HOME/Library/LaunchAgents"', 'mkdir -p "$A"']
        uninst = ["#!/bin/sh"]
        logdir = posixpath.join(cfg.get("state_dir", "~/.aho"), "logs") if not cross else "/tmp"
        for j in native:
            label = f"local.aho.{j.slug}"
            intervals = S.launchd_intervals(j.schedule)    # compute once, reuse in launchd_plist
            args = job_args(j, cfg_path, intervals[1].gated, py, cross)
            em = S.launchd_plist(label, j.schedule, [py, "-m", "aho"] + args, root,
                                 posixpath.join(logdir, f"launchd-{j.slug}.log"), precomputed=intervals)
            f = d / f"{label}.plist"
            f.write_text(em.text, encoding="utf-8")
            pre = "" if j.implemented else "# SKIPPED (stub not implemented): "
            inst += [f'{pre}cp "$(dirname "$0")/{label}.plist" "$A/"',
                     f'{pre}launchctl bootout gui/$(id -u) "$A/{label}.plist" 2>/dev/null || true',
                     f'{pre}launchctl bootstrap gui/$(id -u) "$A/{label}.plist"']
            uninst += [f'launchctl bootout gui/$(id -u) "$HOME/Library/LaunchAgents/{label}.plist" || true',
                       f'rm -f "$HOME/Library/LaunchAgents/{label}.plist"']
            res[j.slug] = {"files": [str(f)], "notes": em.notes, "gated": em.gated}
        (d / "install-launchd.sh").write_text("\n".join(inst) + "\n", encoding="utf-8", newline="\n")
        (d / "uninstall-launchd.sh").write_text("\n".join(uninst) + "\n", encoding="utf-8", newline="\n")
    return res


# -------------------------------------------------------------- manifest
def manifest(jobs: list[Job], cfg: dict, prev: dict | None = None) -> dict:
    """Build the manifest. A job's ``registered_at`` is its monitoring baseline (a run due
    before it is treated as PENDING, not MISSED), so re-running convert must KEEP the
    existing baseline for jobs already registered -- otherwise every convert resets all
    baselines to now and masks a job that has genuinely been missing its runs. The baseline
    is keyed on (name, schedule): a job new to this manifest, OR one whose schedule changed,
    gets a fresh stamp, so a re-scheduled job is not judged against its old slots (which
    would spuriously read as MISSED/NEVER)."""
    t = now_local().isoformat()
    tmo = as_number(cfg.get("job_timeout_minutes"), 60)
    prev_jobs = prev.get("jobs") if isinstance(prev, dict) else None
    if not isinstance(prev_jobs, list):        # {"jobs": null} / a non-list is as useless as no prev
        prev_jobs = []
    prior = {(j.get("name"), j.get("schedule")): j.get("registered_at")
             for j in prev_jobs if isinstance(j, dict) and j.get("registered_at")}
    return {"generated_at": t, "jobs": [
        {"name": j.slug, "title": j.title, "schedule": j.schedule, "kind": "agent" if j.kind == "agent" else "native",
         "verdict": j.verdict, "registered_at": prior.get((j.slug, j.schedule)) or t, "implemented": j.implemented,
         "timeout_minutes": tmo}
        for j in jobs if j.implemented or j.kind == "agent"]}


def load_prev_manifest(*paths: Path) -> dict | None:
    """Return the first readable manifest among ``paths`` (to carry ``registered_at`` forward).
    Checks the out-dir copy first, then the installed ``state_dir`` copy so a clean/rebuilt
    --out still finds the live baseline; a corrupt or half-written file is skipped, not fatal."""
    for p in paths:
        try:
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):     # a top-level array/scalar is as useless as a corrupt file
                    return data
        except (OSError, ValueError):
            continue
    return None


# -------------------------------------------------------------- apply
def _run(cmd, **kw):
    """Subprocess seam (tests monkeypatch this)."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def apply(jobs: list[Job], out: Path, target: str, cfg: dict, linux_backend: str = "systemd") -> list[str]:
    log: list[str] = []
    ready = [j for j in jobs if j.kind in ("native", "builtin") and j.implemented]
    for j in jobs:
        if j.kind == "native" and not j.implemented:
            log.append(f"skip {j.slug}: stub not implemented")
    d = out / target
    if target == "windows":
        for j in ready:
            r = _run(["schtasks", "/Create", "/TN", f"aho\\{j.slug}", "/XML", str(d / f"{j.slug}.xml"), "/F"])
            log.append(f"schtasks {j.slug}: rc={r.returncode} {(r.stdout or r.stderr).strip()[:120]}")
    elif target == "linux" and linux_backend == "cron":
        cur = _run(["crontab", "-l"])
        existing = cur.stdout if cur.returncode == 0 else ""
        keep, inside = [], False
        for ln in existing.splitlines():
            if ln.strip() == CRON_BEGIN:
                inside = True
            elif ln.strip() == CRON_END:
                inside = False
            elif not inside:
                keep.append(ln)
        block = (d / "crontab.txt").read_text(encoding="utf-8")
        new = "\n".join(keep + [block.rstrip("\n")]) + "\n"
        r = _run(["crontab", "-"], input=new)
        log.append(f"crontab install: rc={r.returncode}")
    elif target == "linux":
        u = Path.home() / ".config" / "systemd" / "user"
        u.mkdir(parents=True, exist_ok=True)
        for j in ready:
            for ext in ("service", "timer"):
                shutil.copy2(d / "systemd" / f"aho-{j.slug}.{ext}", u / f"aho-{j.slug}.{ext}")
        log.append(f"daemon-reload: rc={_run(['systemctl', '--user', 'daemon-reload']).returncode}")
        for j in ready:
            r = _run(["systemctl", "--user", "enable", "--now", f"aho-{j.slug}.timer"])
            log.append(f"enable aho-{j.slug}.timer: rc={r.returncode}")
    elif target == "macos":
        la = Path.home() / "Library" / "LaunchAgents"
        la.mkdir(parents=True, exist_ok=True)
        uid = str(os.getuid()) if hasattr(os, "getuid") else "501"
        for j in ready:
            label = f"local.aho.{j.slug}"
            dst = la / f"{label}.plist"
            shutil.copy2(d / f"{label}.plist", dst)
            _run(["launchctl", "bootout", f"gui/{uid}", str(dst)])
            r = _run(["launchctl", "bootstrap", f"gui/{uid}", str(dst)])
            log.append(f"launchctl {label}: rc={r.returncode}")
    # make the manifest visible to the health check
    sd = Path(cfg["state_dir"])
    sd.mkdir(parents=True, exist_ok=True)
    shutil.copy2(out / "manifest.json", sd / "manifest.json")
    log.append(f"manifest -> {sd / 'manifest.json'}")
    return log


# -------------------------------------------------------------- reports
def scan_report_md(verdicts: list[Verdict]) -> str:
    counts = {k: sum(1 for v in verdicts if v.verdict == k) for k in (LOCAL, HYBRID, LLM)}
    lines = ["# aho scan report", "",
             f"{len(verdicts)} workflow(s): **{counts[LOCAL]} LOCAL**, **{counts[HYBRID]} HYBRID**, "
             f"**{counts[LLM]} LLM**", "",
             "| Job | Schedule | Verdict | Reason |", "|---|---|---|---|"]
    for v in verdicts:
        reason = v.reason.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {v.name} | `{v.schedule}` | {v.verdict} | {reason} |")
    lines += ["", "LOCAL = runs on the OS scheduler with no LLM. HYBRID = local runner + an isolated LLM step. "
              "LLM = stays on the agent.", ""]
    return "\n".join(lines)


def conversion_report_md(jobs: list[Job], results: dict[str, dict], skipped: list[str], targets: list[str]) -> str:
    lines = ["# aho conversion report (dry run unless --apply was given)", "",
             f"Targets: {', '.join(targets)}", "",
             "| Job | Kind | Verdict | Schedule | Runs as | Notes |", "|---|---|---|---|---|---|"]
    for j in jobs:
        how = ("agent (unchanged)" if j.kind == "agent" else
               f"`--shell {j.shell}`" if j.shell else
               f"stub `jobs/{j.slug}.py`" + ("" if j.implemented else " (TODO)") if j.stub else "aho built-in")
        notes = list(j.notes)
        for t in targets:
            r = results.get(t, {}).get(j.slug)
            if r:
                notes += [f"{t}: {n}" for n in r["notes"] if "1:1" not in n]
                if r.get("gated"):
                    notes.append(f"{t}: gated (runner checks --only-if-cron)")
        cell = "<br>".join(x.replace("|", "\\|") for x in notes)
        lines.append(f"| {j.title} | {j.kind} | {j.verdict} | `{j.schedule}` | {how} | {cell} |")
    if skipped:
        lines += ["", "## Not schedulable (left on the agent)", ""] + [f"- {s}" for s in skipped]
    lines += ["", "## Next steps", "",
              "1. Implement every stub in `jobs/` and delete its marker line.",
              "2. Re-run `aho convert ... --apply` (only implemented jobs are registered).",
              "3. Once a job runs natively, disable the matching agent cron yourself "
              "(aho never edits or deletes your agent specs).",
              "4. For jobs that stay on the agent, end each run with `python -m aho stamp <job>`.", ""]
    return "\n".join(lines)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
