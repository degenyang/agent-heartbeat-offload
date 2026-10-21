# agent-heartbeat-offload (`aho`)

Move the deterministic part of your AI agent's scheduled jobs onto the OS scheduler
(Windows Task Scheduler, cron, systemd, launchd), and get a heartbeat report that
reaches you even when the agent is down.

Works with any agent that runs jobs on a schedule: Claude Code session crons, Codex CLI,
Hermes, home-grown loops. Python 3.10+, standard library only.

---

## 繁體中文簡介

**呢個 repo 做兩件事：**

1. **篩選同轉換。** 掃描你 AI agent 所有排程工作流程（例如 Claude Code 嘅 cron `.md`
   檔），逐個分類：
   - **LOCAL**：純粹行 script、call API、比較 state、send 固定格式訊息。完全唔使 LLM。
   - **HYBRID**：大部分可以本地行，得一步要 LLM（例如做摘要）。拆開嚟做：本地負責
     fetch 同比較，有需要先叫 LLM。
   - **LLM**：每次都要判斷，或者係控制 agent session 本身。留返喺 agent 度。

   例如 10 個 workflow 入面，可能 8 個可以搬去本地，得 2 個一定要 LLM。之後自動
   偵測作業系統，將 LOCAL（同 HYBRID 嘅本地部分）轉做原生排程：Windows 用 Task
   Scheduler、Linux 用 cron 或 systemd timer、macOS 用 launchd。
   **預設只係 dry run**：寫出排程檔同報告，要加 `--apply` 先會真正登記。
   你原本嘅 agent cron 檔永遠唔會被改或者刪。

2. **Heartbeat（心跳）。** 每個轉換咗嘅 job 都由一個細 runner 包住。每次行完，runner
   寫低時間、exit code 同簡短狀態。另一個 health check（都係原生排程、唔使 LLM）
   讀晒所有 heartbeat，對照每個 job 應該幾時行。過期即係漏咗。然後將報告 send 去你
   嘅 Telegram 或 WhatsApp：每日一份總結，有失敗或者漏咗就即時通知。

**點解要咁做？** LLM agent 會停：供應商出事、CLI crash、session 完咗、quota 用晒、
部機嘅 agent 根本冇開。Agent 自己排嘅 cron（例如 Claude Code session cron）只會喺
session 開住同埋閒置嗰陣先行，所以會靜靜雞漏咗都冇人知。搬去 OS 排程之後，就算
agent（例如 Claude Code CLI）死咗，呢啲 job 照行，heartbeat 報告照到你手。你會**清楚
知道**邊個行咗、邊個冇行。剩低真係要 LLM 判斷嘅 job 先需要靠 agent。

---

## Why this exists

**LLM agents go down.** The provider has an outage. The CLI crashes. The session ends or
gets compacted. You run out of quota. The machine reboots and nobody restarts the agent.

**Agent-scheduled jobs fail without telling you.** A session cron in an agent such as
Claude Code only fires while that session is alive and idle. If the session is busy, the
slot is skipped. If the session is gone, nothing runs and nothing reports the miss. You
only notice days later, when the reminder or alert you were counting on never came.

**Most scheduled agent jobs don't need an LLM.** "Run this script, compare with last
time, send a message if something changed" is plain code. Running it through an LLM costs
quota and adds a single point of failure.

**What `aho` does about it:**

- Jobs that don't need an LLM move to the OS scheduler. They keep running when the agent
  is down.
- Every job writes a heartbeat. A health check, also on the OS scheduler and also with no
  LLM, compares the heartbeats with each job's schedule.
- Missed or failed runs are reported to Telegram or WhatsApp. That report arrives even
  when the agent is down, so you **know** what ran and what didn't.
- Only the jobs that need LLM judgement every run still depend on the agent. The same
  health check can watch those too: the agent runs `aho stamp <job>` at the end of each
  run.

```
             before                                    after
 ┌──────────────────────────┐        ┌──────────────────────────┐   ┌──────────────────────┐
 │ agent session (must be   │        │ OS scheduler (always on)  │   │ agent session         │
 │ alive + idle)            │        │  ├─ 8 LOCAL/HYBRID jobs   │   │  └─ 2 LLM jobs        │
 │  └─ 10 jobs              │  ───►  │  │   └─ aho run → heartbeat│   │      └─ aho stamp     │
 │     (silent misses)      │        │  ├─ aho health check ──────┼───┼──► Telegram/WhatsApp │
 └──────────────────────────┘        │  └─ aho daily digest       │   │      (alerts+digest)  │
                                     └──────────────────────────┘   └──────────────────────┘
```

## Quick start

```bash
git clone <this repo> && cd agent-heartbeat-offload
python -m aho scan examples/workflows            # classify (read-only)
python -m aho convert examples/workflows         # dry run: writes ./aho-out/
python -m pytest                                 # optional: run the tests
```

`pip install -e .` gives you an `aho` command. Running `python -m aho` from the repo
works without installing anything.

Try it on your own specs, for example a Claude Code cron folder:

```bash
python -m aho scan  ~/path/to/your/cron-specs --out /tmp/aho-scan
python -m aho convert ~/path/to/your/cron-specs --out /tmp/aho-out     # still a dry run
```

Then:

1. Read `aho-out/scan_report.md` and `aho-out/conversion_report.md`.
2. Put the job logic into each stub in `aho-out/jobs/`. A stub is created for every job
   whose spec is prose. When the stub works, delete its `AHO_STUB_NOT_IMPLEMENTED`
   line. `aho` never invents job logic.
3. Set up notifications (see below) and check them with `python -m aho test-notify`.
4. Run `python -m aho convert <specs> --apply`. Only jobs that are implemented, plus the
   health check and the digest, get registered.
5. Turn off the matching agent crons yourself. `aho` never edits or deletes your agent specs.
6. For jobs that stay on the agent, add `python -m aho stamp <job>` as the agent's last step.

## Commands

| Command | What it does |
|---|---|
| `aho scan <dir or file> [--out D] [--llm]` | Classify every workflow and write `scan_report.md` and `scan_report.json`. Read-only. |
| `aho convert <dir or file> [--os auto\|windows\|linux\|macos\|all] [--out D] [--linux-backend systemd\|cron] [--python P] [--apply]` | Generate scheduler files, stubs, a manifest and a report. Registers nothing unless you pass `--apply`. `--apply` works only for the current machine's OS. |
| `aho run --job NAME [--shell CMD \| -- argv...] [--notify-output] [--quiet-if REGEX] [--only-if-cron EXPR] [--timeout MIN]` | The job wrapper. Runs the command, logs its output, writes the heartbeat, and alerts right away on failure. |
| `aho heartbeat-check [--mode alert\|digest] [--no-send]` | The health check. `alert` (the default) sends only when there are problems, and doesn't resend the same problem set until `realert_hours` has passed. `digest` always sends. |
| `aho report [--send]` | Print the current status table. With `--send`, also send it. |
| `aho stamp NAME [--exit N] [--summary TEXT]` | Write a heartbeat for a job that runs on the agent. |
| `aho test-notify` | Send a test message through the configured channel. |

## Input formats

- **Markdown specs with frontmatter.** This is the Claude Code cron-spec shape: `name`,
  `schedule` (a 5-field cron expression) and `description` in the frontmatter, with the
  task prose as the body. Files with other suffixes, such as `*.md.paused` or `*.bak`,
  are ignored.
- **A YAML or JSON list** of `{name, schedule, description, task|prompt, command,
  notify_output, quiet_if}`. A job that sets `command` and has no LLM signals runs
  exactly that command, so no stub is needed. See `examples/workflows.yaml` and
  `examples/workflows.json`. PyYAML is used if it's installed. Otherwise a small
  built-in parser reads the documented subset.

## How the classifier decides

It's rule-based. Every verdict comes with a reason that quotes the phrase that triggered
it.

| Signal | Examples | Pushes towards |
|---|---|---|
| runs a script, HTTP/API fetch, state compare, deterministic gate, fixed-format notify, writes a file | `python x.py`, `curl`, "compare with previous run", "obey its output", "send a Telegram message, same format" | LOCAL |
| soft LLM step (can be isolated) | summarise, translate, write-up, draft, explain | HYBRID |
| hard LLM step | analyse, judgement, decide, recommend, research, council or debate, assess, interpret, sub-agent | LLM (two or more of these, or any with no local step) |
| agent-only tool | "connector", `mcp__…` | HYBRID (replace it with a direct API call) |
| agent session control | `/compact`, "this session", "context window" (counted only in the description and the first lines) | LLM |
| schedule isn't a 5-field cron | `on_session_start`, `L`, `W`, `#` | stays on the agent, reported as not schedulable |

Negated mentions such as "no subagent, no council" are ignored. A spec that says "Follow
`other.md`" is classified using the referenced file's text too. If no deterministic step
is found, the verdict is **LLM**: when unsure, `aho` keeps the job on the agent.

`--llm` asks an LLM for a second opinion on borderline jobs. It's off by default and has
no hardcoded provider. Set `AHO_LLM_CMD` to any command that reads a prompt on stdin and
prints an answer, for example `claude -p`, `codex exec -`, `ollama run llama3` or `llm`.
That second opinion can make a verdict more conservative (towards LLM). It never promotes
a job to LOCAL on its own.

## Heartbeats

`aho run` writes `<state_dir>/heartbeats/<job>.json`:

```json
{"job": "disk_space_check", "started_at": "2026-03-10T12:15:00+00:00",
 "finished_at": "2026-03-10T12:15:02+00:00", "exit_code": 0, "status": "ok",
 "duration_s": 1.8, "summary": "last line of output", "host": "..."}
```

The health check finds each job's last scheduled slot (`now - grace_minutes`) from its
cron expression and gives the job one of these statuses:

| Status | Meaning |
|---|---|
| OK | The latest run started at or after the last expected slot and exited 0. |
| MISSED | The latest run is older than the last expected slot, so a scheduled run didn't happen. |
| FAILED | The latest run exited non-zero or timed out. |
| NEVER | No heartbeat at all, although a run was due after the job was registered. |
| PENDING | Just registered. No run is due yet. |
| RUNNING / HUNG | Still running: within its timeout (RUNNING) or well past it (HUNG). |

The health check and the digest write their own heartbeats too. The digest therefore
also tells you whether the health check itself has been running.

## Notifications

Pick a channel with `notify.channel` in `aho.json`, or with the `AHO_NOTIFY_CHANNEL`
environment variable.

| Channel | Status | Settings (env var or `notify.*` key) |
|---|---|---|
| `telegram` | fully supported | `AHO_TELEGRAM_BOT_TOKEN`, `AHO_TELEGRAM_CHAT_ID` |
| `whatsapp_twilio` | adapter | `AHO_TWILIO_ACCOUNT_SID`, `AHO_TWILIO_AUTH_TOKEN`, `AHO_TWILIO_FROM`, `AHO_TWILIO_TO`, optional `AHO_TWILIO_CONTENT_SID` |
| `whatsapp_callmebot` | adapter | `AHO_CALLMEBOT_PHONE`, `AHO_CALLMEBOT_APIKEY` |
| `webhook` | adapter | `AHO_WEBHOOK_URL`. Posts `{"text": ...}` to any gateway, for example n8n, Make, Home Assistant or your own WhatsApp bridge. |
| `stdout` | default | Prints the message. `AHO_DRY_RUN=1` forces this channel. |

There are details and caveats for each channel, including WhatsApp's 24-hour messaging
window. See [docs/notifications.md](docs/notifications.md).

## Configuration

`aho` looks for its config file in this order: `--config`, `$AHO_CONFIG`, `./aho.json`,
then `~/.aho/aho.json`. Copy `examples/aho.example.json` to start. `aho.json` is
gitignored.

Environment variables override the file. OS schedulers start jobs with a minimal
environment: cron and launchd don't read your shell profile. So for scheduled runs, keep
your secrets in a private `aho.json` (`chmod 600`), or in user environment variables on
Windows.

| Key | Default | Meaning |
|---|---|---|
| `state_dir` | `~/.aho` | Holds heartbeats, logs, the manifest and the alert state. |
| `grace_minutes` | 15 | How late a run can be before it counts as missed. |
| `healthcheck_schedule` | `*/30 * * * *` | When the alert-mode health check runs. |
| `digest_schedule` | `0 8 * * *` | When the daily digest runs. |
| `realert_hours` | 12 | How long to wait before resending an unchanged alert. |
| `alert_on_failure` | true | Alert immediately when a job exits non-zero. |
| `job_timeout_minutes` | 60 | How long a job may run before it is killed and reported as timed out. |
| `timezone` | null | An IANA zone appended to systemd `OnCalendar=` lines. |

## Per-OS details

- [docs/windows.md](docs/windows.md): Task Scheduler XML, `schtasks`, the hidden `pythonw` launcher, logon types.
- [docs/linux.md](docs/linux.md): crontab block, systemd user timers, `Persistent=`, lingering.
- [docs/macos.md](docs/macos.md): LaunchAgents, `StartCalendarInterval`, `launchctl bootstrap`.
- [docs/cron-mapping.md](docs/cron-mapping.md): how each cron feature maps to each scheduler, and which expressions **can't** map 1:1.

Short version of the mapping caveats:

- crontab maps 1:1, because it is the source format.
- systemd maps 1:1. Cron's day-of-month OR day-of-week rule becomes two `OnCalendar=` lines.
- launchd has no step, range or list syntax, so `aho` expands each expression into a list
  of `StartCalendarInterval` dicts. For example, `*/15` becomes 4 dicts.
- Task Scheduler triggers are written as start time + repetition interval + a day pattern.
  Some expressions can't be written that way: when both day-of-month and day-of-week must
  match (`*/2` days AND Tuesday), or when more than 48 triggers would be needed. For
  those, `aho` schedules a superset of the cron times and runs the job with
  `--only-if-cron`, so the extra fires exit silently.

The test suite checks every translation. It simulates each scheduler's semantics and
compares the fire times minute by minute with the cron expression, over six weeks, for
20 expressions (steps, lists, ranges, weekday ranges, the day-of-month/day-of-week OR
rule, month limits and macros).

## Safety guarantees

- **Dry run by default.** Only `convert --apply` touches the system scheduler, and only
  on the OS you're running.
- **Your specs are read-only.** `aho` never modifies, renames or deletes them, and a test
  checks this.
- **No invented logic.** A job written as prose gets a stub that does nothing until you
  implement it. Unimplemented stubs are never registered.
- **No secrets in the repo.** Credentials come only from environment variables or the
  gitignored `aho.json`.
- Registered tasks use the `aho\` folder (Windows), `aho-*.timer` units or a marked
  crontab block (Linux), or `local.aho.*` labels (macOS), so you can find and remove all
  of them. Each output folder also contains an uninstall script.

## Limitations

- The classifier matches keywords. It's a screener that shows its reasons, not an oracle,
  so review the report. A word used in an unrelated sense (such as a proper noun
  containing "Analysis") can push a job towards LLM. The bias is deliberately
  conservative.
- Cron is evaluated in the machine's local time zone. `CRON_TZ` and per-job time zones
  aren't parsed.
- Non-standard cron tokens (`L`, `W`, `#`, `?`, seconds fields) are reported as not
  schedulable instead of being guessed.
- Jobs that need an agent-only tool, such as an MCP connector, are HYBRID. You have to
  replace that tool with a direct API call.
- `--apply` for systemd, launchd and cron is covered by unit tests with mocked
  subprocess calls. Review the generated files before running it.

## License

MIT. See [LICENSE](LICENSE).
