import json
import sys
from pathlib import Path

import pytest

from aho import cli, convert
from aho.heartbeat import read_heartbeats
from aho.runner import run_job, stamp

EX = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AHO_DRY_RUN", "1")
    return {"state_dir": str(tmp_path / "state"), "alert_on_failure": True, "notify": {"channel": "stdout"}}


def test_run_success_writes_heartbeat(cfg):
    code = run_job(cfg, "ok", argv=[sys.executable, "-c", "print('all good')"])
    hb = read_heartbeats(cfg["state_dir"])["ok"]
    assert code == 0 and hb["status"] == "ok" and hb["exit_code"] == 0 and hb["summary"] == "all good"
    assert (Path(cfg["state_dir"]) / "logs" / "ok.log").exists()


def test_run_failure_alerts(cfg, capsys):
    code = run_job(cfg, "bad", argv=[sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])
    hb = read_heartbeats(cfg["state_dir"])["bad"]
    assert code == 3 and hb["status"] == "failed"
    assert "FAILED" in capsys.readouterr().out


def test_run_timeout(cfg):
    code = run_job(cfg, "slow", argv=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_min=0.02)
    assert code == 124 and read_heartbeats(cfg["state_dir"])["slow"]["status"] == "timeout"


def test_run_missing_binary(cfg):
    assert run_job(cfg, "nobin", argv=["definitely-not-a-real-binary-xyz"]) == 127


def test_notify_output_and_quiet(cfg, capsys):
    run_job(cfg, "n", argv=[sys.executable, "-c", "print('price drop!')"], notify_output=True)
    assert "price drop!" in capsys.readouterr().out
    run_job(cfg, "q", argv=[sys.executable, "-c", "print('NONE')"], notify_output=True, quiet_regex=r"^NONE$")
    assert "[aho notify]" not in capsys.readouterr().out


def test_only_if_cron_gate(cfg):
    # Feb 30 never exists -> the gate always says "not a real slot": no run, no heartbeat
    assert run_job(cfg, "g", argv=[sys.executable, "-c", "raise SystemExit(9)"], only_if_cron="0 0 30 2 *") == 0
    assert "g" not in read_heartbeats(cfg["state_dir"])


def test_stamp(cfg):
    stamp(cfg, "agent_job", 0, "done")
    assert read_heartbeats(cfg["state_dir"])["agent_job"]["status"] == "ok"


def test_cli_convert_all_os_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AHO_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "out"
    assert cli.main(["convert", str(EX / "workflows"), "--os", "all", "--out", str(out)]) == 0
    assert "dry run" in capsys.readouterr().out
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    kinds = {j["name"]: j["kind"] for j in man["jobs"]}
    assert kinds["industry_news_digest"] == "agent"
    assert kinds["aho_healthcheck"] == "native"
    assert "disk_space_check" not in kinds          # stub not implemented yet -> not monitored
    assert (out / "jobs" / "disk_space_check.py").exists()
    assert (out / "windows" / "aho_healthcheck.xml").read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    assert "OnCalendar=" in (out / "linux" / "systemd" / "aho-aho_digest.timer").read_text()
    assert (out / "macos" / "local.aho.aho_digest.plist").exists()
    assert "# >>> aho" in (out / "linux" / "crontab.txt").read_text()
    assert not (tmp_path / "state" / "manifest.json").exists()   # dry run registers nothing


def test_convert_never_overwrites_stub_and_respects_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("AHO_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "out"
    cli.main(["convert", str(EX / "workflows"), "--os", "linux", "--out", str(out)])
    stub = out / "jobs" / "disk_space_check.py"
    stub.write_text("print('implemented')\n", encoding="utf-8")       # user implemented it
    cli.main(["convert", str(EX / "workflows"), "--os", "linux", "--out", str(out)])
    assert stub.read_text(encoding="utf-8") == "print('implemented')\n"
    man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "disk_space_check" in {j["name"] for j in man["jobs"]}
    cron = (out / "linux" / "crontab.txt").read_text()
    assert any("disk_space_check" in ln and not ln.startswith("#") for ln in cron.splitlines())


def test_apply_windows_uses_schtasks_only_for_ready_jobs(tmp_path, monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "SUCCESS", ""

    monkeypatch.setattr(convert, "_run", lambda cmd, **kw: calls.append(cmd) or R())
    cfg = {"state_dir": str(tmp_path / "state"), "job_timeout_minutes": 60}
    from aho.classify import scan
    from aho.specs import load_path
    wfs = load_path(EX / "workflows.yaml")
    jobs, _ = convert.plan_jobs(wfs, scan(wfs), tmp_path, cfg)
    convert.write_json(tmp_path / "manifest.json", convert.manifest(jobs, cfg))
    convert.emit(jobs, tmp_path, "windows", cfg, python="C:\\Py\\pythonw.exe")
    log = convert.apply(jobs, tmp_path, "windows", cfg)
    names = [c[3] for c in calls]
    assert names == ["aho\\rotate_logs", "aho\\sync_notes_folder", "aho\\aho_healthcheck", "aho\\aho_digest"]
    assert all(c[0] == "schtasks" and "/XML" in c for c in calls)
    assert (tmp_path / "state" / "manifest.json").exists() and log


def test_heartbeat_check_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AHO_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AHO_DRY_RUN", "1")
    (tmp_path / "manifest.json").write_text(json.dumps({"jobs": [
        {"name": "x", "title": "X", "schedule": "* * * * *", "registered_at": "2020-01-01T00:00:00"}]}))
    assert cli.main(["heartbeat-check", "--mode", "alert"]) == 0
    out = capsys.readouterr().out
    assert "ALERT" in out and "NEVER" in out
    assert cli.main(["heartbeat-check", "--mode", "digest"]) == 0
    assert "digest" in capsys.readouterr().out
    assert "aho_healthcheck" in read_heartbeats(tmp_path)
