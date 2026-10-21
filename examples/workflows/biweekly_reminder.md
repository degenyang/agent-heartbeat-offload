---
name: Biweekly Timesheet Reminder
schedule: "3 9 * * 1"
description: Every other Monday at 09:03 remind me to submit my timesheet.
---

1. Run the deterministic parity check and obey its output:
   ```bash
   python3 -c "import datetime as d; print('FIRE' if (d.date.today()-d.date(2026,1,5)).days//7 % 2 == 0 else 'SKIP')"
   ```
2. `SKIP` -> stop, no message.
3. `FIRE` -> send ONE Telegram message, fixed text: "Reminder: submit your timesheet today."
