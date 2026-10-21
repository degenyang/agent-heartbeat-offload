---
name: Disk Space Check
schedule: "15 */6 * * *"
description: Every 6 hours, warn when any disk is above 90% full.
---

## Task

1. Run the checker:

   ```bash
   python3 scripts/disk_check.py --threshold 90
   ```

2. If the output is `NONE`, stop. Stay silent.
3. Otherwise send ONE Telegram message with the script output, same format every time:
   `Disk warning: <mount> at <pct>%`.
