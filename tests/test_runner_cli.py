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


def test_only_if_cron_runs_a_late_catch_up(cfg, monkeypatch):
    # Machine asleep at the 09:00 slot; the scheduler fires the catch-up at 09:07. An exact
    # minute match would drop it. No prior heartbeat for this slot -> it must run.
    import datetime as dt

    from aho import runner
    late = dt.datetime(2026, 3, 10, 9, 7).astimezone()
    monkeypatch.setattr(runner, "now_local", lambda: late)
    code = run_job(cfg, "late", argv=[sys.executable, "-c", "print('ran')"], only_if_cron="0 9 * * *")
    hb = read_heartbeats(cfg["state_dir"]).get("late")
    assert code == 0 and hb and hb["status"] == "ok"


def test_only_if_cron_drops_extra_fire_after_slot_ran(cfg, monkeypatch):
    # The 09:00 slot already ran; a superset fire at 09:30 for the same slot must be dropped.
    import datetime as dt

    from aho import runner
    from aho.heartbeat import write_heartbeat
    write_heartbeat(cfg["state_dir"], "dup",
                    started_at=dt.datetime(2026, 3, 10, 9, 0).astimezone().isoformat(),
                    finished_at=None, exit_code=0, status="ok", duration_s=0, summary="first")
    extra = dt.datetime(2026, 3, 10, 9, 30).astimezone()
    monkeypatch.setattr(runner, "now_local", lambda: extra)
    code = run_job(cfg, "dup", argv=[sys.executable, "-c", "raise SystemExit(9)"], only_if_cron="0 9 * * *")
    hb = read_heartbeats(cfg["state_dir"])["dup"]
    assert code == 0 and hb["summary"] == "first"   # untouched: the extra fire never ran


def test_only_if_cron_drops_far_late_fire_without_history(cfg, monkeypatch):
    # No heartbeat to prove a missed slot: a fire whose real slot is a day+ back is a genuine
    # superset extra (e.g. a Monday schedule firing on Tuesday), drop it.
    import datetime as dt

    from aho import runner
    tue = dt.datetime(2026, 3, 10, 9, 0).astimezone()       # Tuesday; "0 9 * * 1" slot was Monday
    monkeypatch.setattr(runner, "now_local", lambda: tue)
    code = run_job(cfg, "nohist", argv=[sys.executable, "-c", "raise SystemExit(9)"], only_if_cron="0 9 * * 1")
    assert code == 0 and "nohist" not in read_heartbeats(cfg["state_dir"])


def test_only_if_cron_bootstraps_late_first_fire_within_catchup(cfg, monkeypatch):
    # A gated job with no heartbeat yet whose catch-up fire lands past grace but within the
    # catch-up window must still run its FIRST time -- otherwise it never writes a heartbeat and
    # is silenced forever. 09:45 is 45m past the 09:00 slot (grace 15, catchup 120).
    import datetime as dt

    from aho import runner
    late = dt.datetime(2026, 3, 10, 9, 45).astimezone()
    monkeypatch.setattr(runner, "now_local", lambda: late)
    code = run_job(cfg, "boot", argv=[sys.executable, "-c", "print('ran')"], only_if_cron="0 9 * * *")
    hb = read_heartbeats(cfg["state_dir"]).get("boot")
    assert code == 0 and hb and hb["status"] == "ok"        # ran and wrote the first heartbeat


def test_only_if_cron_runs_long_catch_up_with_history(cfg, monkeypatch):
    # A heartbeat proving today's 09:00 slot has NOT run (last run was yesterday) lets even a
    # long-delayed catch-up fire run.
    import datetime as dt

    from aho import runner
    from aho.heartbeat import write_heartbeat
    write_heartbeat(cfg["state_dir"], "catch",
                    started_at=dt.datetime(2026, 3, 9, 9, 0).astimezone().isoformat(),
                    finished_at=None, exit_code=0, status="ok", duration_s=0, summary="yesterday")
    late = dt.datetime(2026, 3, 10, 11, 0).astimezone()
    monkeypatch.setattr(runner, "now_local", lambda: late)
    code = run_job(cfg, "catch", argv=[sys.executable, "-c", "print('ran')"], only_if_cron="0 9 * * *")
    hb = read_heartbeats(cfg["state_dir"])["catch"]
    assert code == 0 and hb["status"] == "ok" and hb["summary"] != "yesterday"


def test_only_if_cron_survives_non_dict_heartbeat(cfg, monkeypatch):
    # A valid-JSON but non-object heartbeat (null / array / scalar) must not crash the gate:
    # .get() on it would raise AttributeError, which the except did not catch.
    import datetime as dt

    from aho import runner
    from aho.heartbeat import hb_dir
    d = hb_dir(cfg["state_dir"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "weird.json").write_text("null", encoding="utf-8")
    far = dt.datetime(2026, 3, 10, 11, 0).astimezone()      # 2h past 09:00, no usable history
    monkeypatch.setattr(runner, "now_local", lambda: far)
    code = run_job(cfg, "weird", argv=[sys.executable, "-c", "print('ran')"], only_if_cron="0 9 * * *")
    assert code == 0        # no AttributeError; unusable heartbeat -> no history -> far-late fire dropped


def test_timeout_zero_means_no_timeout(cfg):
    # An explicit --timeout 0 must NOT silently become the default; 0 => no timeout, so a
    # command that outlives a tiny configured default still succeeds.
    cfg = dict(cfg, job_timeout_minutes=0.01)       # 0.6s default; the bug used this for --timeout 0
    code = run_job(cfg, "z", argv=[sys.executable, "-c", "import time; time.sleep(1.2)"], timeout_min=0)
    assert code == 0 and read_heartbeats(cfg["state_dir"])["z"]["status"] == "ok"


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


def test_manifest_preserves_registered_at(tmp_path):
    # Re-running convert must keep each already-registered job's monitoring baseline,
    # otherwise a job that has genuinely been missing its runs is masked as PENDING.
    from aho.classify import scan
    from aho.specs import load_path
    cfg = {"state_dir": str(tmp_path), "job_timeout_minutes": 60}
    wfs = load_path(EX / "workflows.yaml")
    jobs, _ = convert.plan_jobs(wfs, scan(wfs), tmp_path, cfg)
    first = convert.manifest(jobs, cfg)
    reg = {j["name"]: j["registered_at"] for j in first["jobs"]}
    assert reg
    second = convert.manifest(jobs, cfg, prev=first)
    for j in second["jobs"]:
        assert j["registered_at"] == reg[j["name"]]      # baseline kept, not reset to now


def test_load_prev_manifest_falls_back_and_skips_corrupt(tmp_path):
    # The baseline must survive a clean/rebuilt --out: fall back to the installed state_dir
    # copy, and never crash convert on a corrupt/half-written manifest.
    out = tmp_path / "out" / "manifest.json"
    state = tmp_path / "state" / "manifest.json"
    out.parent.mkdir(parents=True)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"jobs": [{"name": "x", "registered_at": "2020-01-01T00:00:00"}]}))
    assert convert.load_prev_manifest(out, state)["jobs"][0]["registered_at"] == "2020-01-01T00:00:00"
    out.write_text("{ not valid json")
    got = convert.load_prev_manifest(out, state)           # corrupt out -> skipped, uses state_dir
    assert got and got["jobs"][0]["name"] == "x"
    assert convert.load_prev_manifest(tmp_path / "nope.json") is None


def test_load_prev_manifest_skips_non_dict_json(tmp_path):
    # A top-level array/scalar is valid JSON but not a manifest; it must be skipped (returning it
    # would make manifest()'s (prev or {}).get("jobs") crash), falling back to the real dict.
    out = tmp_path / "out" / "manifest.json"
    state = tmp_path / "state" / "manifest.json"
    out.parent.mkdir(parents=True)
    state.parent.mkdir(parents=True)
    out.write_text("[1, 2, 3]")        # valid JSON, wrong shape
    state.write_text(json.dumps({"jobs": [{"name": "x", "registered_at": "2020-01-01T00:00:00"}]}))
    got = convert.load_prev_manifest(out, state)
    assert got and got["jobs"][0]["name"] == "x"      # array skipped, fell back to the dict
    assert convert.load_prev_manifest(out) is None    # array only -> None, never the bare list


def test_manifest_resets_baseline_when_schedule_changes(tmp_path):
    # registered_at is keyed on (name, schedule): a job whose schedule changed must get a fresh
    # baseline, or it would be judged against its old slots and read as a spurious MISSED/NEVER.
    from aho.classify import scan
    from aho.specs import load_path
    cfg = {"state_dir": str(tmp_path), "job_timeout_minutes": 60}
    wfs = load_path(EX / "workflows.yaml")
    jobs, _ = convert.plan_jobs(wfs, scan(wfs), tmp_path, cfg)
    first = convert.manifest(jobs, cfg)
    # pin every baseline to a distinct past value so a reset is visible (now_local()'s resolution
    # is too coarse on Windows to tell two back-to-back stamps apart).
    OLD = "2020-01-01T00:00:00"
    prev = {"jobs": [dict(j, registered_at=OLD) for j in first["jobs"]]}
    target = next(j for j in jobs if j.slug in {x["name"] for x in prev["jobs"]})
    target.schedule = "0 0 1 1 *" if target.schedule != "0 0 1 1 *" else "0 12 * * *"
    second = {j["name"]: j for j in convert.manifest(jobs, cfg, prev=prev)["jobs"]}
    assert second[target.slug]["registered_at"] != OLD   # rescheduled -> fresh baseline, not the old slot
    for name, j in second.items():
        if name != target.slug:
            assert j["registered_at"] == OLD             # the rest keep theirs


def test_windows_timeout_rounds_fractional_up_not_to_zero(tmp_path):
    # job_timeout_minutes=0.5: the runner enforces 30s, so the OS limit and manifest must NOT
    # read as "no timeout" -- int(0.5)==0 (the old bug) silently disabled both.
    from aho.classify import scan
    from aho.specs import load_path
    cfg = {"state_dir": str(tmp_path / "state"), "job_timeout_minutes": 0.5}
    wfs = load_path(EX / "workflows.yaml")
    jobs, _ = convert.plan_jobs(wfs, scan(wfs), tmp_path, cfg)
    convert.emit(jobs, tmp_path, "windows", cfg, python="C:\\Py\\pythonw.exe")
    xml = (tmp_path / "windows" / "aho_healthcheck.xml").read_text(encoding="utf-16")
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" not in xml   # not disabled
    assert "<ExecutionTimeLimit>PT6M</ExecutionTimeLimit>" in xml       # ceil(0.5)+5 = 6 min
    assert convert.manifest(jobs, cfg)["jobs"][0]["timeout_minutes"] == 0.5   # float kept, not int()


@pytest.mark.parametrize("bad_prev", [{"jobs": None}, {"jobs": ["x"]}, {"jobs": [None]}, {"jobs": "oops"}, []])
def test_manifest_survives_malformed_prev(tmp_path, bad_prev):
    # load_prev_manifest only checks the TOP-LEVEL type; manifest() must still not crash on a
    # prev whose "jobs" is null / not a list / holds non-dict items.
    from aho.classify import scan
    from aho.specs import load_path
    cfg = {"state_dir": str(tmp_path), "job_timeout_minutes": 60}
    wfs = load_path(EX / "workflows.yaml")
    jobs, _ = convert.plan_jobs(wfs, scan(wfs), tmp_path, cfg)
    man = convert.manifest(jobs, cfg, prev=bad_prev)       # must not raise
    assert man["jobs"] and all(j["registered_at"] for j in man["jobs"])


def test_manifest_carries_only_well_formed_prev_entries(tmp_path):
    from aho.classify import scan
    from aho.specs import load_path
    cfg = {"state_dir": str(tmp_path), "job_timeout_minutes": 60}
    wfs = load_path(EX / "workflows.yaml")
    jobs, _ = convert.plan_jobs(wfs, scan(wfs), tmp_path, cfg)
    good = convert.manifest(jobs, cfg)["jobs"][0]
    prev = {"jobs": [None, "x", {"no": "name"}, dict(good, registered_at="2019-05-05T00:00:00")]}
    out = {j["name"]: j for j in convert.manifest(jobs, cfg, prev=prev)["jobs"]}
    assert out[good["name"]]["registered_at"] == "2019-05-05T00:00:00"   # the one valid entry carried


def test_config_coerces_string_numbers(tmp_path, monkeypatch):
    from aho import config
    p = tmp_path / "aho.json"
    p.write_text(json.dumps({"job_timeout_minutes": "90", "grace_minutes": "20",
                             "catchup_minutes": "30", "realert_hours": "6"}))
    cfg = config.load_config(str(p))
    assert cfg["job_timeout_minutes"] == 90.0 and cfg["grace_minutes"] == 20
    assert cfg["catchup_minutes"] == 30 and cfg["realert_hours"] == 6.0
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"job_timeout_minutes": "soon"}))        # unparseable -> default
    assert config.load_config(str(bad))["job_timeout_minutes"] == 60


def test_run_tolerates_string_timeout_in_cfg(tmp_path, monkeypatch):
    # A string job_timeout_minutes must not crash the runner's 60*tmo arithmetic.
    monkeypatch.setenv("AHO_DRY_RUN", "1")
    cfg = {"state_dir": str(tmp_path / "state"), "alert_on_failure": True,
           "notify": {"channel": "stdout"}, "job_timeout_minutes": "60"}
    code = run_job(cfg, "s", argv=[sys.executable, "-c", "print('ok')"])
    assert code == 0 and read_heartbeats(cfg["state_dir"])["s"]["status"] == "ok"


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
