# Windows: Task Scheduler

`aho convert --os windows` writes one Task Scheduler XML file per job to
`aho-out/windows/<job>.xml`. It also writes `register.cmd` and `unregister.cmd`.

## What the XML contains

- **Action:** `pythonw.exe -m aho run --job <job> ...`, with `WorkingDirectory` set to the
  repo root. `pythonw.exe` has no console, so nothing flashes on screen. The job's own
  command is started with `CREATE_NO_WINDOW` as well.
- **Triggers:** one or more `CalendarTrigger`s. Each one has:
  - a time of day (`StartBoundary`, local time);
  - an optional `Repetition` (`Interval` + `Duration`), used for steps such as `*/15` or
    `50 */2`;
  - a day pattern: `ScheduleByDay`, `ScheduleByWeek`, `ScheduleByMonth` or
    `ScheduleByMonthDayOfWeek`. The last one is used when weekdays are limited to certain
    months.
- **Settings:**
  - `StartWhenAvailable=true`: a run missed while the PC was off or asleep starts as soon
    as possible afterwards.
  - `MultipleInstancesPolicy=IgnoreNew`.
  - `ExecutionTimeLimit` is the job timeout + 5 min.
  - The task runs on battery and is hidden.
- **Principal:** `InteractiveToken`, which means the task runs only while you are logged
  on. This is the right choice when jobs need your user profile, mapped drives or
  cloud-sync drives. If you need it to run while logged off, change it in Task Scheduler
  to "Run whether user is logged on or not". That stores your password, or use S4U,
  which has no network credentials.

## Registering

Dry run (the default) only writes the files. Review them, then do one of these:

```bat
aho-out\windows\register.cmd
```

or:

```bat
python -m aho convert <specs> --apply
```

Both run `schtasks /Create /TN "aho\<job>" /XML <file> /F` for every job that's
implemented. Jobs whose stub isn't implemented yet appear as `rem SKIPPED` lines. To
remove the tasks, run `unregister.cmd`, which runs `schtasks /Delete /TN "aho\<job>" /F`.

You can inspect a task with `schtasks /Query /TN "aho\<job>" /V /FO LIST` or in Task
Scheduler (taskschd.msc) under the `aho` folder.

## Mapping limits

- A single task can have at most 48 triggers. `aho` packs the cron times into as few
  triggers as possible: one repeating trigger when the times are evenly spaced, otherwise
  one per minute value or per hour value. If more than 48 would still be needed, it falls
  back to one repeating trigger on a superset of the times, and the runner filters with
  `--only-if-cron`.
- Task Scheduler can't express "this day-of-month AND this weekday". Cron uses AND when
  one of the two fields starts with `*`, for example `0 7 */2 * 2`. Such jobs fire every
  day at the cron times and are gated by the runner.
- Cron's OR rule ("the 1st and 15th, or any Monday") maps exactly onto two trigger
  families.
