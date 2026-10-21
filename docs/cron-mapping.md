# Cron expression mapping

`aho` reads standard 5-field cron: `minute hour day-of-month month day-of-week`. It
supports:

- `*`, numbers, lists (`1,5`), ranges (`1-5`) and steps (`*/15`, `1-30/5`, and `5/10`,
  which means `5-59/10`);
- month and weekday names (`jan`, `mon`), with weekday `7` meaning Sunday;
- the macros `@hourly`, `@daily`, `@midnight`, `@weekly`, `@monthly`, `@yearly` and
  `@annually`.

It follows the Vixie-cron day rule. If **both** day-of-month and day-of-week are
restricted and neither starts with `*`, a day matches when **either** field matches.
Otherwise both must match.

| Feature | crontab | systemd `OnCalendar=` | launchd `StartCalendarInterval` | Task Scheduler |
|---|---|---|---|---|
| single values | 1:1 | 1:1 | 1:1 | 1:1 |
| lists `1,5` | 1:1 | 1:1 (`01,05`) | one dict per value | one trigger per value, or a repetition if evenly spaced |
| ranges `1-5` | 1:1 | list, or `Mon..Fri` for weekdays | one dict per value | one trigger per value, a repetition, or a `DaysOfWeek` set |
| steps `*/15` | 1:1 | `00/15` | one dict per value (no step syntax) | `Repetition` (`PT15M`) |
| range + step `1-30/5` | 1:1 | expanded list | expanded dicts | repetition (evenly spaced) |
| month restriction | 1:1 | 1:1 | `Month` key | `Months` in `ScheduleByMonth` / `ScheduleByMonthDayOfWeek` |
| DOM **or** DOW (both restricted) | 1:1 | two `OnCalendar=` lines | separate `Day` and `Weekday` dicts | two trigger families |
| DOM **and** DOW (one starts with `*`, both restrict days, e.g. `*/2` + `2`) | 1:1 | 1:1 (systemd ANDs them) | **not expressible**: gated superset | **not expressible**: gated superset |
| more than 48 distinct trigger groups | n/a | n/a | n/a | **gated superset** (one repeating trigger) |
| `L`, `W`, `#`, `?`, seconds, years | not supported by `aho`: reported as not schedulable | | | |

**Gated superset** means the OS scheduler fires at every time that *could* match, and
`aho run --only-if-cron "<expr>"` exits immediately, without writing a heartbeat, when
the current minute isn't a real cron slot. The conversion report marks every gated job.

**Time zones.** Every scheduler here uses the machine's local time zone, except systemd
when you set `timezone`. If the agent's crons assumed a different zone, convert the
expressions before running `aho`, or set the machine's time zone.

**Missed runs after downtime:**

| Scheduler | Behaviour |
|---|---|
| cron | Skips them. |
| systemd | Runs once with `Persistent=true`. |
| launchd | Runs once on wake. |
| Task Scheduler | Runs once with `StartWhenAvailable`. |

Either way, the health check shows what actually happened.

The test suite (`tests/test_schedulers.py`) simulates systemd, launchd and Task Scheduler
and compares their fire times minute by minute with cron, over 45 days, for 20
expressions.
