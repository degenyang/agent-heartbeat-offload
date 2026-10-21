"""Translate a cron expression into native OS scheduler definitions.

Targets
  * Windows   -> Task Scheduler XML (CalendarTrigger + Repetition), registered with
                 ``schtasks /Create /XML``; action runs ``pythonw.exe`` (no console window).
  * Linux     -> a crontab line (1:1, cron *is* the native format) and a systemd
                 user ``.service`` + ``.timer`` pair (OnCalendar=).
  * macOS     -> a launchd LaunchAgent plist (StartCalendarInterval array).

When a schedule cannot be represented exactly, the emitter falls back to a
*gated* schedule: the OS fires on a superset of the cron times and the runner is
given ``--only-if-cron "<expr>"`` so it exits silently (no heartbeat) on the
extra fires. Every such case is reported in ``notes``.
"""
from __future__ import annotations

import math
import shlex
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from .cronexpr import Cron, parse

FULL_MIN = frozenset(range(60))
FULL_HOUR = frozenset(range(24))
FULL_DOM = frozenset(range(1, 32))
FULL_MONTH = frozenset(range(1, 13))
FULL_DOW = frozenset(range(7))

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
SD_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

MAX_WIN_TRIGGERS = 48          # Task Scheduler hard limit per task
MAX_LAUNCHD_DICTS = 500        # sanity cap; launchd itself has no documented limit


@dataclass
class Emitted:
    text: str
    notes: list[str] = field(default_factory=list)
    gated: bool = False         # runner must be given --only-if-cron


# --------------------------------------------------------------------- helpers
def _ap(values: list[int]) -> tuple[int, int, int] | None:
    """(start, step, count) if sorted values form an arithmetic progression."""
    if not values:
        return None
    if len(values) == 1:
        return values[0], 0, 1
    step = values[1] - values[0]
    if all(b - a == step for a, b in zip(values, values[1:])):
        return values[0], step, len(values)
    return None


def _compress(values: list[int], sep_range: str) -> str:
    """[1,2,3,7] -> '1..3,7' (systemd) ; used for readability only."""
    out, i = [], 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[j + 1] == values[j] + 1:
            j += 1
        if j - i >= 2:
            out.append(f"{values[i]}{sep_range}{values[j]}")
        else:
            out.extend(str(v) for v in values[i:j + 1])
        i = j + 1
    return ",".join(out)


def _day_mode(c: Cron) -> str:
    """'or'  -> cron OR-semantics between DOM and DOW (both restricted with no '*').
       'and' -> both must match."""
    return "or" if (not c.dom_star and not c.dow_star) else "and"


def _iso_dur(minutes: int) -> str:
    d, rem = divmod(minutes, 1440)
    h, m = divmod(rem, 60)
    s = "P" + (f"{d}D" if d else "")
    if h or m or not d:
        s += "T" + (f"{h}H" if h else "") + (f"{m}M" if m else "")
    return s


# ===================================================================== Windows
def _time_groups(c: Cron) -> list[tuple[int, int, int]] | None:
    """Cover the set of times-of-day with as few (start_min, interval_min, count)
    arithmetic progressions as possible (each becomes one trigger)."""
    tods = sorted(h * 60 + m for h, m in c.times_of_day())
    ap = _ap(tods)
    if ap:
        return [ap]
    cands = []
    # strategy A: per minute value, hours form a progression
    groups = []
    for m in sorted(c.minute):
        hrs = sorted(c.hour)
        a = _ap(hrs)
        if a:
            groups.append((a[0] * 60 + m, a[1] * 60, a[2]))
        else:
            groups.extend((h * 60 + m, 0, 1) for h in hrs)
    cands.append(groups)
    # strategy B: per hour value, minutes form a progression
    groups = []
    for h in sorted(c.hour):
        mins = sorted(c.minute)
        a = _ap(mins)
        if a:
            groups.append((h * 60 + a[0], a[1], a[2]))
        else:
            groups.extend((h * 60 + m, 0, 1) for m in mins)
    cands.append(groups)
    return min(cands, key=len)


def _win_day_families(c: Cron) -> list[str] | None:
    months = sorted(c.month)
    month_xml = "<Months>" + "".join(f"<{MONTHS[m - 1]} />" for m in months) + "</Months>"
    dows = sorted(c.dow)
    dow_xml = "<DaysOfWeek>" + "".join(f"<{DAYS[d]} />" for d in dows) + "</DaysOfWeek>"

    def monthly(doms):
        return ("<ScheduleByMonth><DaysOfMonth>" + "".join(f"<Day>{d}</Day>" for d in doms)
                + "</DaysOfMonth>" + month_xml + "</ScheduleByMonth>")

    def by_dow():
        if c.month == FULL_MONTH:
            return f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval>{dow_xml}</ScheduleByWeek>"
        weeks = "".join(f"<Week>{w}</Week>" for w in ("1", "2", "3", "4", "Last"))
        return f"<ScheduleByMonthDayOfWeek><Weeks>{weeks}</Weeks>{dow_xml}{month_xml}</ScheduleByMonthDayOfWeek>"

    daily = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    dom_r, dow_r = c.dom != FULL_DOM, c.dow != FULL_DOW
    if _day_mode(c) == "or":
        return [monthly(sorted(c.dom)), by_dow()]
    if dom_r and dow_r:
        return None                                   # AND of both: not expressible
    if dow_r:
        return [by_dow()]
    if dom_r or c.month != FULL_MONTH:
        return [monthly(sorted(c.dom))]
    return [daily]


def _win_trigger(start_min: int, interval: int, count: int, sched: str, year: int) -> str:
    h, m = divmod(start_min, 60)
    rep = ""
    if count > 1:
        dur = interval * (count - 1) + 1
        rep = (f"<Repetition><Interval>{_iso_dur(interval)}</Interval>"
               f"<Duration>{_iso_dur(dur)}</Duration>"
               f"<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>")
    return (f"    <CalendarTrigger>{rep}<StartBoundary>{year}-01-01T{h:02d}:{m:02d}:00</StartBoundary>"
            f"<Enabled>true</Enabled>{sched}</CalendarTrigger>")


def _gate_fallback_groups(c: Cron) -> list[tuple[int, int, int]]:
    """Every-g-minutes repetition covering all times of day (superset)."""
    tods = sorted(h * 60 + m for h, m in c.times_of_day())
    g = 0
    for a, b in zip(tods, tods[1:]):
        g = math.gcd(g, b - a)
    g = g or 1440
    return [(tods[0], g, (tods[-1] - tods[0]) // g + 1)]


def windows_task_xml(cron_expr: str, command: str, arguments: str, workdir: str,
                     description: str = "", year: int = 2026,
                     time_limit_min: int = 60) -> Emitted:
    c = parse(cron_expr)
    notes: list[str] = []
    gated = False
    fams = _win_day_families(c)
    groups = _time_groups(c)
    if fams is None:
        gated = True
        notes.append("Task Scheduler cannot AND a day-of-month with a day-of-week; "
                     "fires daily at the cron times and the runner gates on the full expression.")
        fams = ["<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"]
    if len(groups) * len(fams) > MAX_WIN_TRIGGERS:
        gated = True
        groups = _gate_fallback_groups(c)
        notes.append(f"Needs more than {MAX_WIN_TRIGGERS} triggers; using one repeating trigger "
                     "on a superset of the times + runner gate.")
    if _day_mode(c) == "or":
        notes.append("cron OR-semantics for day-of-month/day-of-week mapped to two trigger families.")
    trig = "\n".join(_win_trigger(s, i, n, f, year) for f in fams for s, i, n in groups)
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>{_iso_dur(time_limit_min)}</ExecutionTimeLimit>
  </Settings>
  <Triggers>
{trig}
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    return Emitted(xml, notes, gated)


# ======================================================================= cron
def crontab_line(cron_expr: str, command: str) -> Emitted:
    parse(cron_expr)  # validate
    return Emitted(f"{cron_expr} {command}", ["crontab: 1:1 (native format)."])


# ==================================================================== systemd
def _sd_field(values: frozenset, full: frozenset, width: int = 2) -> str:
    if values == full:
        return "*"
    v = sorted(values)
    a = _ap(v)
    if a and a[2] > 2 and a[0] + a[1] * (a[2] - 1) + a[1] > max(full):
        return f"{a[0]:0{width}d}/{a[1]}"          # open-ended repetition, e.g. 0/15
    return ",".join(f"{x:0{width}d}" for x in v)


def _sd_dow(values: frozenset) -> str:
    v = sorted(values)
    a = _ap(v)
    if a and a[1] == 1 and a[2] > 2:
        return f"{SD_DAYS[v[0]]}..{SD_DAYS[v[-1]]}"
    return ",".join(SD_DAYS[d] for d in v)


def systemd_oncalendar(cron_expr: str, tz: str | None = None) -> Emitted:
    c = parse(cron_expr)
    notes = []
    mon = _sd_field(c.month, FULL_MONTH)
    hr = _sd_field(c.hour, FULL_HOUR)
    mi = _sd_field(c.minute, FULL_MIN)
    dom = _sd_field(c.dom, FULL_DOM)
    tail = f" {tz}" if tz else ""
    lines = []
    if _day_mode(c) == "or":
        notes.append("cron OR-semantics for day-of-month/day-of-week mapped to two OnCalendar= lines.")
        lines.append(f"*-{mon}-{dom} {hr}:{mi}:00{tail}")
        lines.append(f"{_sd_dow(c.dow)} *-{mon}-* {hr}:{mi}:00{tail}")
    else:
        dow = "" if c.dow == FULL_DOW else _sd_dow(c.dow) + " "
        lines.append(f"{dow}*-{mon}-{dom} {hr}:{mi}:00{tail}")
    return Emitted("\n".join(f"OnCalendar={x}" for x in lines), notes)


def systemd_units(name: str, cron_expr: str, exec_start: str, workdir: str,
                  description: str = "", tz: str | None = None) -> tuple[str, str, Emitted]:
    cal = systemd_oncalendar(cron_expr, tz)
    service = f"""[Unit]
Description=aho job {name}: {description}

[Service]
Type=oneshot
WorkingDirectory={workdir}
ExecStart={exec_start}
"""
    timer = f"""[Unit]
Description=aho timer for {name} (cron: {cron_expr})

[Timer]
{cal.text}
Persistent=true
Unit=aho-{name}.service

[Install]
WantedBy=timers.target
"""
    return service, timer, cal


# ===================================================================== launchd
def launchd_intervals(cron_expr: str, cap: int = MAX_LAUNCHD_DICTS) -> tuple[list[dict] | None, Emitted]:
    """Return (list of StartCalendarInterval dicts, info). None => use StartInterval=60."""
    c = parse(cron_expr)
    notes = []
    fields = [("Minute", c.minute, FULL_MIN), ("Hour", c.hour, FULL_HOUR),
              ("Month", c.month, FULL_MONTH)]

    def product(base_fields):
        dicts = [{}]
        for key, vals, full in base_fields:
            if vals == full:
                continue
            dicts = [dict(d, **{key: v}) for d in dicts for v in sorted(vals)]
        return dicts

    day_fields = []
    if _day_mode(c) == "or":
        day_fields = [[("Day", c.dom, FULL_DOM)], [("Weekday", c.dow, FULL_DOW)]]
        notes.append("cron OR-semantics for day-of-month/day-of-week: separate dict sets for Day and Weekday.")
    else:
        dom_r, dow_r = c.dom != FULL_DOM, c.dow != FULL_DOW
        if dom_r and dow_r:
            return None, Emitted("", ["launchd cannot AND Day with Weekday (it ORs them); "
                                      "gated fallback required."], True)
        day_fields = [[("Day", c.dom, FULL_DOM), ("Weekday", c.dow, FULL_DOW)]]
    out = []
    for df in day_fields:
        out.extend(product(fields + df))
    if out == [{}]:
        return None, Emitted("", ["every minute: StartInterval=60."])
    if len(out) > 1:
        notes.append(f"launchd has no step/list/range syntax: expanded to {len(out)} StartCalendarInterval dicts.")
    if len(out) > cap:
        return None, Emitted("", [f"expansion is {len(out)} dicts (> {cap}); gated fallback required."], True)
    return out, Emitted("", notes)


def _plist_value(v) -> str:
    if isinstance(v, bool):
        return "<true/>" if v else "<false/>"
    if isinstance(v, int):
        return f"<integer>{v}</integer>"
    if isinstance(v, dict):
        return "<dict>" + "".join(f"<key>{escape(k)}</key>{_plist_value(x)}" for k, x in v.items()) + "</dict>"
    if isinstance(v, list):
        return "<array>" + "".join(_plist_value(x) for x in v) + "</array>"
    return f"<string>{escape(str(v))}</string>"


def launchd_plist(label: str, cron_expr: str, program_args: list[str], workdir: str,
                  log_path: str) -> Emitted:
    dicts, info = launchd_intervals(cron_expr)
    gated = info.gated
    body = {
        "Label": label,
        "ProgramArguments": program_args,
        "WorkingDirectory": workdir,
        "RunAtLoad": False,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }
    if dicts is None:
        if gated:
            c = parse(cron_expr)
            g = _gate_fallback_groups(c)[0]
            # superset: every minute-of-day progression, any day; runner gates the rest
            mins = range(g[0], g[0] + g[1] * g[2], g[1] or 1)
            dicts = [{"Hour": m // 60, "Minute": m % 60} for m in mins]
            if len(dicts) > MAX_LAUNCHD_DICTS:
                body["StartInterval"] = 60
                dicts = None
        else:
            body["StartInterval"] = 60
    if dicts is not None:
        body["StartCalendarInterval"] = dicts
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
             '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
             '<plist version="1.0">', _plist_value(body), "</plist>", ""]
    return Emitted("\n".join(lines), info.notes, gated)


def quote_cmd(args: list[str], windows: bool = False) -> str:
    if windows:
        import subprocess
        return subprocess.list2cmdline(args)
    return " ".join(shlex.quote(a) for a in args)
