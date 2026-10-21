# macOS: launchd LaunchAgents

`aho convert --os macos` writes `aho-out/macos/local.aho.<job>.plist`, along with
`install-launchd.sh` and `uninstall-launchd.sh`.

## What the plist contains

- `ProgramArguments`: `[python3, -m, aho, run, --job, <job>, ...]`, with
  `WorkingDirectory` set to the repo root.
- `StartCalendarInterval`: an **array of dicts** using the keys `Minute`, `Hour`, `Day`,
  `Weekday` and `Month`. A missing key is a wildcard, and 0 and 7 both mean Sunday.
- `StandardOutPath` / `StandardErrorPath`: the launchd-level log. The job's own output
  goes to `<state_dir>/logs/<job>.log`.
- For `* * * * *`, `StartInterval=60` is used instead.

## Why there are so many dicts

launchd has no step, range or list syntax. `aho` expands the cron expression into the
cartesian product of the fields that are restricted. Fields that are `*` are left out.

| cron | dicts |
|---|---|
| `*/15 * * * *` | 4 (`Minute` 0, 15, 30, 45) |
| `47 22 * * 1-5` | 5 (one per weekday) |
| `7 2,8,14,20 * * *` | 4 |
| `0 7 1,15 * 1` | 3 (`Day` 1, `Day` 15, `Weekday` 1). Cron's OR rule is kept as separate dicts. |

launchd starts a job if **either** `Day` or `Weekday` matches. It can't express cron's
AND case, where one of the two fields starts with `*` (for example `0 7 */2 * 2`). Such
jobs, and expansions of more than 500 dicts, get a superset schedule and are filtered by
the runner's `--only-if-cron`.

## Sleep behaviour

Unlike cron, launchd runs a job missed during sleep when the Mac wakes up. If several
runs were missed, they are combined into one run.

## Installing

Run `sh aho-out/macos/install-launchd.sh` or `python -m aho convert <specs> --apply`.
Both do the following for each plist:

```bash
cp local.aho.<job>.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.aho.<job>.plist   # ignore errors
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.aho.<job>.plist
```

Check a job with `launchctl print gui/$(id -u)/local.aho.<job>`. To remove everything,
run `uninstall-launchd.sh`.

`/usr/bin/python3` is the default interpreter. On a fresh Mac it asks you to install the
Command Line Tools. Pass `--python /opt/homebrew/bin/python3` (or similar) to use
another interpreter.
