"""Translation tests. Besides checking the text, each generated schedule is fed to a
small simulator of the target scheduler's semantics and compared minute-by-minute
with the cron expression over ~6 weeks."""
import calendar
import datetime as dt
import plistlib
import re
import xml.etree.ElementTree as ET

import pytest

from aho import schedulers as S
from aho.cronexpr import parse

START = dt.datetime(2026, 1, 1)
DAYS = 45

EXPRS = [
    "*/15 * * * *", "50 */2 * * *", "7,37 * * * *", "7 2,8,14,20 * * *", "3 9,21 * * 1",
    "47 22 * * 1-5", "13 4 * * 2-6", "0 7 1,15 * *", "0 7 1,15 * 1", "0 9 * 1,2 *",
    "0 9 * 2 1", "*/5 9-17 * * 1-5", "0,10,25 * * * *", "0 9,12,18 * * *", "1-10/3 8 * * *",
    "0 7 */2 * 2", "@daily", "15 */6 * * *", "30 6 1 * *", "0 0 * * 0,6",
]


def cron_fires(expr):
    c = parse(expr)
    out = set()
    t = START
    end = START + dt.timedelta(days=DAYS)
    while t < end:
        if c.matches(t):
            out.add(t)
        t += dt.timedelta(minutes=1)
    return out


# ------------------------------------------------------------------ Windows sim
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
DAYNAMES = S.DAYS
MONTHNAMES = S.MONTHS


def _dur(s):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", s)
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def win_fires(xml):
    root = ET.fromstring(xml.split("?>", 1)[1])
    fires = set()
    for trig in root.find("t:Triggers", NS):
        sb = trig.find("t:StartBoundary", NS).text
        h, m = int(sb[11:13]), int(sb[14:16])
        rep = trig.find("t:Repetition", NS)
        offsets = [0]
        if rep is not None:
            iv, du = _dur(rep.find("t:Interval", NS).text), _dur(rep.find("t:Duration", NS).text)
            offsets = list(range(0, du, iv))
        sched = [c for c in trig if c.tag.split("}")[1].startswith("ScheduleBy")][0]
        kind = sched.tag.split("}")[1]

        def names(tag):
            el = sched.find(f"t:{tag}", NS)
            return {c.tag.split("}")[1] for c in el} if el is not None else None

        for i in range(DAYS):
            day = (START + dt.timedelta(days=i)).date()
            dname = DAYNAMES[day.isoweekday() % 7]
            mname = MONTHNAMES[day.month - 1]
            if kind == "ScheduleByDay":
                ok = True
            elif kind == "ScheduleByWeek":
                ok = dname in names("DaysOfWeek")
            elif kind == "ScheduleByMonth":
                doms = {int(d.text) for d in sched.find("t:DaysOfMonth", NS)}
                ok = day.day in doms and mname in names("Months")
            else:  # ScheduleByMonthDayOfWeek
                weeks = {w.text for w in sched.find("t:Weeks", NS)}
                last = calendar.monthrange(day.year, day.month)[1]
                wk = {str((day.day - 1) // 7 + 1)} if day.day <= 28 else set()
                if day.day > last - 7:
                    wk.add("Last")
                ok = dname in names("DaysOfWeek") and mname in names("Months") and bool(wk & weeks)
            if ok:
                base = dt.datetime.combine(day, dt.time(h, m))
                fires.update(base + dt.timedelta(minutes=o) for o in offsets)
    return {f for f in fires if f < START + dt.timedelta(days=DAYS)}


@pytest.mark.parametrize("expr", EXPRS)
def test_windows_matches_cron(expr):
    em = S.windows_task_xml(expr, "pythonw.exe", "-m aho", "C:\\x")
    want, got = cron_fires(expr), win_fires(em.text)
    if em.gated:
        assert want <= got, "gated schedule must be a superset"
    else:
        assert got == want


def test_windows_no_timeout_execution_limit():
    # time_limit_min <= 0 => PT0S, which disables Task Scheduler's execution-time limit.
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in S.windows_task_xml(
        "0 9 * * *", "p", "", "d", time_limit_min=0).text
    assert "<ExecutionTimeLimit>PT1H</ExecutionTimeLimit>" in S.windows_task_xml(
        "0 9 * * *", "p", "", "d", time_limit_min=60).text


def test_windows_step_uses_repetition():
    em = S.windows_task_xml("50 */2 * * *", "pythonw.exe", "", "C:\\x")
    assert "<Interval>PT2H</Interval>" in em.text
    assert em.text.count("<CalendarTrigger>") == 1
    assert "T00:50:00" in em.text


def test_windows_or_semantics_and_gate():
    em = S.windows_task_xml("0 7 1,15 * 1", "p", "", "d")
    assert "ScheduleByMonth" in em.text and "ScheduleByWeek" in em.text and not em.gated
    g = S.windows_task_xml("0 7 */2 * 2", "p", "", "d")
    assert g.gated and any("AND" in n for n in g.notes)


def test_windows_too_many_triggers_falls_back_to_gate():
    expr = "0,7,19,33,51 0,1,3,5,7,9,12,13,20,22,23 * * *"  # 55 triggers needed -> gated
    em = S.windows_task_xml(expr, "p", "", "d")
    assert em.text.count("<CalendarTrigger>") <= S.MAX_WIN_TRIGGERS
    assert em.gated
    assert cron_fires(expr) <= win_fires(em.text)


# ------------------------------------------------------------------ systemd sim
SD = {n: i for i, n in enumerate(S.SD_DAYS)}


def _sd_set(tok, lo, hi):
    if tok == "*":
        return set(range(lo, hi + 1))
    out = set()
    for part in tok.split(","):
        if "/" in part:
            a, step = part.split("/")
            out.update(range(int(a), hi + 1, int(step)))
        elif ".." in part:
            a, b = part.split("..")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _sd_dows(tok):
    out = set()
    for part in tok.split(","):
        if ".." in part:
            a, b = part.split("..")
            out.update(range(SD[a], SD[b] + 1))
        else:
            out.add(SD[part])
    return out


def sd_fires(text):
    fires = set()
    for line in text.splitlines():
        spec = line.split("=", 1)[1].split()
        dows = _sd_dows(spec[0]) if len(spec) == 3 else set(range(7))
        date, time_ = spec[-2], spec[-1]
        _, mon, dom = date.split("-")
        hh, mm, _ = time_.split(":")
        months, doms = _sd_set(mon, 1, 12), _sd_set(dom, 1, 31)
        hours, mins = _sd_set(hh, 0, 23), _sd_set(mm, 0, 59)
        for i in range(DAYS):
            day = (START + dt.timedelta(days=i)).date()
            if day.month in months and day.day in doms and (day.isoweekday() % 7) in dows:
                for h in hours:
                    for m in mins:
                        fires.add(dt.datetime.combine(day, dt.time(h, m)))
    return fires


@pytest.mark.parametrize("expr", EXPRS)
def test_systemd_matches_cron(expr):
    em = S.systemd_oncalendar(expr)
    assert sd_fires(em.text) == cron_fires(expr)


def test_systemd_text():
    assert S.systemd_oncalendar("*/15 * * * *").text == "OnCalendar=*-*-* *:00/15:00"
    assert S.systemd_oncalendar("47 22 * * 1-5").text == "OnCalendar=Mon..Fri *-*-* 22:47:00"
    assert S.systemd_oncalendar("7 2,8,14,20 * * *").text == "OnCalendar=*-*-* 02/6:07:00"
    two = S.systemd_oncalendar("0 7 1,15 * 1").text.splitlines()
    assert len(two) == 2
    assert S.systemd_oncalendar("0 8 * * *", tz="Europe/Berlin").text.endswith(" Europe/Berlin")


def test_systemd_units():
    svc, tmr, _ = S.systemd_units("demo", "0 8 * * *", "/usr/bin/python3 -m aho run --job demo -- true", "/opt/aho")
    assert "Type=oneshot" in svc and "WorkingDirectory=/opt/aho" in svc
    assert "Persistent=true" in tmr and "OnCalendar=*-*-* 08:00:00" in tmr


# ------------------------------------------------------------------ launchd sim
def launchd_fires(plist_text):
    d = plistlib.loads(plist_text.encode())
    every_min = "StartInterval" in d
    dicts = d.get("StartCalendarInterval", [])
    fires = set()
    t = START
    end = START + dt.timedelta(days=DAYS)
    while t < end:
        if every_min:
            fires.add(t)
        else:
            wd = t.isoweekday() % 7
            for x in dicts:
                ok = all(x[k] == v for k, v in (("Minute", t.minute), ("Hour", t.hour), ("Month", t.month))
                         if k in x)
                if "Day" in x and "Weekday" in x:
                    ok = ok and (x["Day"] == t.day or x["Weekday"] % 7 == wd)
                else:
                    ok = ok and x.get("Day", t.day) == t.day and x.get("Weekday", wd) % 7 == wd
                if ok:
                    fires.add(t)
                    break
        t += dt.timedelta(minutes=1)
    return fires


@pytest.mark.parametrize("expr", EXPRS)
def test_launchd_matches_cron(expr):
    em = S.launchd_plist("local.test", expr, ["/usr/bin/python3", "-m", "aho"], "/opt/aho", "/tmp/x.log")
    want, got = cron_fires(expr), launchd_fires(em.text)
    if em.gated:
        assert want <= got
    else:
        assert got == want


def test_launchd_step_expansion():
    dicts, info = S.launchd_intervals("*/15 * * * *")
    assert dicts == [{"Minute": 0}, {"Minute": 15}, {"Minute": 30}, {"Minute": 45}]
    assert any("expanded" in n for n in info.notes)
    dicts, _ = S.launchd_intervals("0 7 1,15 * 1")
    assert {"Minute": 0, "Hour": 7, "Day": 1} in dicts and {"Minute": 0, "Hour": 7, "Weekday": 1} in dicts
    dicts, _ = S.launchd_intervals("* * * * *")
    assert dicts is None
    em = S.launchd_plist("x", "* * * * *", ["a"], "/", "/tmp/l")
    assert plistlib.loads(em.text.encode())["StartInterval"] == 60


def test_crontab_passthrough():
    assert S.crontab_line("*/5 * * * *", "echo hi").text == "*/5 * * * * echo hi"
