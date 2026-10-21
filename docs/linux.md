# Linux: cron or systemd user timers

`aho convert --os linux` writes both options. With `--apply`, the
`--linux-backend systemd|cron` flag (default `systemd`) chooses which one to install.

## crontab (`aho-out/linux/crontab.txt`)

cron is the source format, so the schedule is copied as-is. Each line looks like this:

```
*/10 * * * * cd '/path/to/agent-heartbeat-offload' && /usr/bin/python3 -m aho run --job website_uptime ... >/dev/null 2>&1
```

- All lines sit between `# >>> aho (agent-heartbeat-offload) >>>` and
  `# <<< aho (agent-heartbeat-offload) <<<`. `--apply --linux-backend cron` replaces only
  that block and keeps the rest of your crontab. To remove it, delete the block with
  `crontab -e`.
- `%` is escaped as `\%`, because cron treats a bare `%` as a newline.
- cron uses the system time zone. Some cron implementations (cronie) support a
  `CRON_TZ=` line. You can add one at the top of the block yourself.
- cron doesn't run missed jobs after downtime. The health check will report them as
  MISSED.
- The environment is minimal, so put secrets in `aho.json` (with `--config`), not in
  your shell profile.

## systemd user timers (`aho-out/linux/systemd/`)

Each job gets two units: `aho-<job>.service` (`Type=oneshot`) and `aho-<job>.timer`.

- `OnCalendar=` is generated from the cron expression:

  | cron | `OnCalendar=` |
  |---|---|
  | `*/15 * * * *` | `*-*-* *:00/15:00` |
  | `47 22 * * 1-5` | `Mon..Fri *-*-* 22:47:00` |
  | `7 2,8,14,20 * * *` | `*-*-* 02/6:07:00` |

- Cron's day-of-month OR day-of-week rule becomes two `OnCalendar=` lines. A timer fires
  when any of its lines matches.
- `Persistent=true` runs a missed slot once when the machine comes back up.
- When `timezone` is set in `aho.json`, it is appended to each line (for example
  `... Europe/Berlin`). This needs a reasonably recent systemd. Check your version with
  `man systemd.time`.
- `%` is escaped as `%%` in `ExecStart=`.

To install, run `sh aho-out/linux/install-systemd.sh` or
`python -m aho convert <specs> --apply`. This copies the units to
`~/.config/systemd/user`, runs `systemctl --user daemon-reload` and
`systemctl --user enable --now aho-<job>.timer`. To remove them, run
`uninstall-systemd.sh`.

User timers stop when you log out unless lingering is enabled:

```bash
loginctl enable-linger "$USER"
```

You can check the result with `systemctl --user list-timers 'aho-*'` and
`systemd-analyze calendar '<OnCalendar expression>'`.
