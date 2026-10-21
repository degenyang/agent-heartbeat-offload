---
name: TLS Certificate Expiry
schedule: "0 7 1,15 * *"
description: On the 1st and 15th, warn if a certificate expires within 21 days.
---

1. Run `python3 scripts/cert_days_left.py www.example.com api.example.com`.
2. For every host where days_left < 21, send a Telegram alert in the fixed format
   `Cert for <host> expires in <n> days`.
3. Otherwise stay silent.
