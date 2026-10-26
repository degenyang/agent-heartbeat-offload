import datetime as dt

from aho.heartbeat import (alert_signature, evaluate, evaluate_job, format_report, read_heartbeats,
                           record_alert, should_alert, write_heartbeat)

NOW = dt.datetime(2026, 3, 10, 13, 5)                  # Tuesday
JOB = {"name": "j", "title": "Job", "schedule": "50 */2 * * *",
       "registered_at": "2026-03-01T00:00:00", "timeout_minutes": 30}


def hb(started, code=0, status=None):
    return {"started_at": started.isoformat(), "exit_code": code,
            "status": status or ("ok" if code == 0 else "failed"), "summary": "line"}


def test_ok():
    s = evaluate_job(JOB, hb(dt.datetime(2026, 3, 10, 12, 50, 3)), NOW)
    assert s.status == "OK" and not s.bad


def test_missed_when_last_run_before_expected_slot():
    s = evaluate_job(JOB, hb(dt.datetime(2026, 3, 10, 10, 50)), NOW)
    assert s.status == "MISSED" and "12:50" in s.detail


def test_within_grace_is_not_missed():
    # 12:50 slot, now 13:00 with 15 min grace -> expected slot is still 10:50
    s = evaluate_job(JOB, hb(dt.datetime(2026, 3, 10, 10, 50)), dt.datetime(2026, 3, 10, 13, 0))
    assert s.status == "OK"


def test_failed():
    s = evaluate_job(JOB, hb(dt.datetime(2026, 3, 10, 12, 50), code=2), NOW)
    assert s.status == "FAILED" and "exit 2" in s.detail


def test_never_and_pending():
    assert evaluate_job(JOB, None, NOW).status == "NEVER"
    fresh = dict(JOB, registered_at="2026-03-10T13:00:00")
    assert evaluate_job(fresh, None, NOW).status == "PENDING"


def test_running_and_hung():
    r = evaluate_job(JOB, hb(dt.datetime(2026, 3, 10, 12, 50), code=None, status="running"), NOW)
    assert r.status == "RUNNING"
    h = evaluate_job(JOB, hb(dt.datetime(2026, 3, 10, 10, 0), code=None, status="running"), NOW)
    assert h.status == "HUNG"


def test_weekly_job_not_stale_midweek():
    weekly = dict(JOB, schedule="3 9 * * 1")
    s = evaluate_job(weekly, hb(dt.datetime(2026, 3, 9, 9, 3)), NOW)
    assert s.status == "OK"
    s = evaluate_job(weekly, hb(dt.datetime(2026, 3, 2, 9, 3)), NOW)
    assert s.status == "MISSED"


def test_no_timeout_never_hung():
    # timeout_minutes <= 0 means "no timeout": a long-running job is RUNNING, never HUNG.
    j = dict(JOB, timeout_minutes=0)
    s = evaluate_job(j, hb(dt.datetime(2026, 3, 10, 0, 0), code=None, status="running"), NOW)
    assert s.status == "RUNNING"


def test_string_timeout_in_manifest_does_not_crash():
    # A hand-edited manifest may carry timeout_minutes as a JSON string; evaluate_job must
    # coerce it rather than crash on the `tmo_min > 0` comparison.
    j = dict(JOB, timeout_minutes="30")
    h = evaluate_job(j, hb(dt.datetime(2026, 3, 10, 10, 0), code=None, status="running"), NOW)
    assert h.status == "HUNG"
    j2 = dict(JOB, timeout_minutes="nonsense")      # unparseable -> default 60 min
    r = evaluate_job(j2, hb(dt.datetime(2026, 3, 10, 12, 50), code=None, status="running"), NOW)
    assert r.status == "RUNNING"


def test_timezone_aware_timestamps():
    aware = dt.datetime(2026, 3, 10, 12, 50).astimezone()
    s = evaluate_job(JOB, {"started_at": aware.isoformat(), "exit_code": 0, "status": "ok"}, NOW)
    assert s.status == "OK"


def test_write_read_and_report(tmp_path):
    write_heartbeat(tmp_path, "j", started_at=dt.datetime(2026, 3, 10, 12, 50).isoformat(),
                    exit_code=0, status="ok", summary="fine")
    hbs = read_heartbeats(tmp_path)
    assert hbs["j"]["exit_code"] == 0
    man = {"jobs": [JOB, dict(JOB, name="k", title="Other")]}
    st = evaluate(man, hbs, NOW)
    assert [s.status for s in st] == ["OK", "NEVER"]
    digest = format_report(st, "digest", host="h")
    assert "1/2 OK" in digest and "Other" in digest
    alert = format_report(st, "alert", host="h")
    assert "Other" in alert and "[OK]" not in alert


def test_alert_dedupe(tmp_path):
    man = {"jobs": [JOB]}
    st = evaluate(man, {}, NOW)
    sig = alert_signature(st)
    assert sig
    assert should_alert(tmp_path, sig, NOW, 12)
    record_alert(tmp_path, sig, NOW)
    assert not should_alert(tmp_path, sig, NOW + dt.timedelta(hours=1), 12)
    assert should_alert(tmp_path, sig, NOW + dt.timedelta(hours=13), 12)
    assert alert_signature(evaluate(man, {"j": hb(dt.datetime(2026, 3, 10, 12, 50))}, NOW)) == ""


def test_alert_signature_ignores_run_time():
    # A job that keeps failing writes a new started_at each cycle. The signature must stay
    # the same for the same set of problems, or realert_hours throttling is defeated.
    man = {"jobs": [JOB]}
    s1 = evaluate(man, {"j": hb(dt.datetime(2026, 3, 10, 12, 50), code=2)}, dt.datetime(2026, 3, 10, 13, 5))
    s2 = evaluate(man, {"j": hb(dt.datetime(2026, 3, 10, 14, 50), code=2)}, dt.datetime(2026, 3, 10, 15, 5))
    assert s1[0].status == s2[0].status == "FAILED"
    assert s1[0].last_run != s2[0].last_run                      # the run times really differ
    assert alert_signature(s1) == alert_signature(s2) != ""      # ... but the signature does not
