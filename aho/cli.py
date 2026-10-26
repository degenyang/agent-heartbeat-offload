"""Command-line interface: ``python -m aho <command>`` (or ``aho`` when installed)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .classify import scan as classify_scan
from .config import load_config
from .convert import (OSES, apply, conversion_report_md, detect_os, emit, load_prev_manifest,
                      manifest, plan_jobs, scan_report_md, write_json)
from .heartbeat import (alert_signature, evaluate, format_report, now_local, read_heartbeats,
                        record_alert, should_alert, write_heartbeat)
from .notify import make_notifier, notify
from .runner import main_guard, run_job, stamp
from .specs import load_path


def _table(verdicts) -> str:
    w = max([len(v.name) for v in verdicts] + [4])
    rows = [f"{'JOB':<{w}}  {'VERDICT':<7}  REASON"]
    for v in verdicts:
        rows.append(f"{v.name:<{w}}  {v.verdict:<7}  {v.reason}")
    return "\n".join(rows)


def cmd_scan(a, cfg) -> int:
    wfs = load_path(a.path)
    if not wfs:
        print(f"no workflow specs found in {a.path}", file=sys.stderr)
        return 1
    verdicts = classify_scan(wfs, use_llm=a.llm)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "scan_report.json", [v.to_dict() for v in verdicts])
    (out / "scan_report.md").write_text(scan_report_md(verdicts), encoding="utf-8")
    print(_table(verdicts))
    print(f"\nreport: {out / 'scan_report.md'}")
    return 0


def cmd_convert(a, cfg) -> int:
    wfs = load_path(a.path)
    if not wfs:
        print(f"no workflow specs found in {a.path}", file=sys.stderr)
        return 1
    verdicts = classify_scan(wfs, use_llm=a.llm)
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    targets = list(OSES) if a.os == "all" else [detect_os() if a.os == "auto" else a.os]
    jobs, skipped = plan_jobs(wfs, verdicts, out, cfg)
    write_json(out / "scan_report.json", [v.to_dict() for v in verdicts])
    (out / "scan_report.md").write_text(scan_report_md(verdicts), encoding="utf-8")
    man_path = out / "manifest.json"
    # carry registered_at forward: prefer this out-dir's manifest, else the installed one
    prev_man = load_prev_manifest(man_path, Path(cfg["state_dir"]) / "manifest.json")
    write_json(man_path, manifest(jobs, cfg, prev_man))
    cfg_path = cfg.get("_config_path")
    results = {t: emit(jobs, out, t, cfg, a.python, cfg_path) for t in targets}
    (out / "conversion_report.md").write_text(conversion_report_md(jobs, results, skipped, targets),
                                             encoding="utf-8")
    print(_table(verdicts))
    print(f"\ngenerated for {', '.join(targets)} in {out}")
    if a.apply:
        if len(targets) != 1 or targets[0] != detect_os():
            print("--apply only works for this machine's OS (use --os auto)", file=sys.stderr)
            return 2
        for line in apply(jobs, out, targets[0], cfg, a.linux_backend):
            print("apply:", line)
    else:
        print("dry run: nothing was registered. Review the files, then re-run with --apply.")
    return 0


def cmd_run(a, cfg) -> int:
    argv = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
    if not argv and a.shell is None:
        print("run: give a command after -- or use --shell", file=sys.stderr)
        return 2
    return run_job(cfg, a.job, argv=argv or None, shell=a.shell, timeout_min=a.timeout,
                   only_if_cron=a.only_if_cron, notify_output=a.notify_output, quiet_regex=a.quiet_if)


def _load_manifest(a, cfg) -> dict:
    p = Path(a.manifest) if a.manifest else Path(cfg["state_dir"]) / "manifest.json"
    if not p.is_file():
        raise SystemExit(f"manifest not found: {p} (run `aho convert --apply`, or pass --manifest)")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_check(a, cfg) -> int:
    man = _load_manifest(a, cfg)
    now = now_local()
    statuses = evaluate(man, read_heartbeats(cfg["state_dir"]), now, int(cfg.get("grace_minutes", 15)))
    text = format_report(statuses, a.mode)
    sent = False
    if a.mode == "digest":
        sent = a.no_send or notify(cfg, text)
    else:
        sig = alert_signature(statuses)
        if sig and should_alert(cfg["state_dir"], sig, now, float(cfg.get("realert_hours", 12))):
            sent = a.no_send or notify(cfg, text)
            if sent and not a.no_send:
                record_alert(cfg["state_dir"], sig, now)
    if a.no_send or a.mode == "alert":
        print(text)
    name = "aho_digest" if a.mode == "digest" else "aho_healthcheck"
    t = now.isoformat()
    ok = sent or a.mode == "alert"
    write_heartbeat(cfg["state_dir"], name, started_at=t, finished_at=t, exit_code=0 if ok else 1,
                    status="ok" if ok else "failed", duration_s=0,
                    summary=f"{sum(s.bad for s in statuses)} problem(s)" + ("" if ok else "; notify failed"))
    return 0 if ok else 1


def cmd_report(a, cfg) -> int:
    man = _load_manifest(a, cfg)
    statuses = evaluate(man, read_heartbeats(cfg["state_dir"]), now_local(), int(cfg.get("grace_minutes", 15)))
    text = format_report(statuses, "digest")
    print(text)
    if a.send:
        return 0 if notify(cfg, text) else 1
    return 0


def cmd_stamp(a, cfg) -> int:
    stamp(cfg, a.job, a.exit, a.summary or "")
    return 0


def cmd_test_notify(a, cfg) -> int:
    ok = make_notifier(cfg).send(a.text)
    print("sent" if ok else "FAILED")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aho", description="agent-heartbeat-offload: move deterministic "
                                "agent workflows to the OS scheduler, with heartbeats.")
    p.add_argument("--config", help="path to aho.json (default: $AHO_CONFIG, ./aho.json, ~/.aho/aho.json)")
    p.add_argument("--version", action="version", version=f"aho {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="classify workflow specs (read-only)")
    s.add_argument("path", help="directory of .md specs, or a .yaml/.json list")
    s.add_argument("--out", default="aho-out", help="report directory (default ./aho-out)")
    s.add_argument("--llm", action="store_true", help="ask $AHO_LLM_CMD to double-check borderline jobs")
    s.set_defaults(fn=cmd_scan)

    c = sub.add_parser("convert", help="generate native scheduler files (dry run unless --apply)")
    c.add_argument("path")
    c.add_argument("--os", default="auto", choices=["auto", "all", *OSES])
    c.add_argument("--out", default="aho-out")
    c.add_argument("--apply", action="store_true", help="actually register the jobs on this machine")
    c.add_argument("--linux-backend", default="systemd", choices=["systemd", "cron"])
    c.add_argument("--python", help="interpreter path to put in scheduler entries")
    c.add_argument("--llm", action="store_true")
    c.set_defaults(fn=cmd_convert)

    r = sub.add_parser("run", help="run a command as job NAME and write its heartbeat")
    r.add_argument("--job", required=True)
    r.add_argument("--shell", help="command string run through the shell")
    r.add_argument("--timeout", type=float, help="minutes")
    r.add_argument("--only-if-cron", help="exit silently unless now matches this cron expression")
    r.add_argument("--notify-output", action="store_true", help="send stdout to the channel on success")
    r.add_argument("--quiet-if", help="regex: if stdout matches, do not send it")
    r.add_argument("argv", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    h = sub.add_parser("heartbeat-check", help="evaluate heartbeats; alert (default) or send a digest")
    h.add_argument("--mode", default="alert", choices=["alert", "digest"])
    h.add_argument("--manifest")
    h.add_argument("--no-send", action="store_true", help="print instead of sending")
    h.set_defaults(fn=cmd_check)

    rp = sub.add_parser("report", help="print the current heartbeat status")
    rp.add_argument("--manifest")
    rp.add_argument("--send", action="store_true")
    rp.set_defaults(fn=cmd_report)

    st = sub.add_parser("stamp", help="write a heartbeat for an agent-run job")
    st.add_argument("job")
    st.add_argument("--exit", type=int, default=0)
    st.add_argument("--summary")
    st.set_defaults(fn=cmd_stamp)

    t = sub.add_parser("test-notify", help="send a test message through the configured channel")
    t.add_argument("--text", default="aho test message: notifications work.")
    t.set_defaults(fn=cmd_test_notify)
    return p


def main(argv: list[str] | None = None) -> int:
    main_guard()
    a = build_parser().parse_args(argv)
    cfg = load_config(a.config)
    return a.fn(a, cfg)


if __name__ == "__main__":
    sys.exit(main())
