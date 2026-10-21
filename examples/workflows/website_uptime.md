---
name: Website Uptime
schedule: "*/10 * * * *"
description: Every 10 minutes check that the homepage answers 200.
---

Run:

```bash
curl -s -o /dev/null -w "%{http_code}" https://www.example.com/
```

Compare the status code with the previous run (stored in `state/uptime.json`).
Send a Telegram alert only when the status changes (UP -> DOWN or DOWN -> UP).
