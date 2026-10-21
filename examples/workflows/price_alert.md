---
name: Coffee Bean Price Alert
schedule: "0 9,21 * * *"
description: Twice a day, alert if the tracked product drops below the target price.
---

1. `curl -s "https://api.example.com/v1/products/12345/price"` and read `price` from the JSON.
2. Compare with the threshold in `state/price_target.json` (target 18.00).
3. If price <= target and it is lower than the previous run, send a Telegram message
   using the template: `Price drop: <product> now $<price> (target $<target>)`.
4. Save the price to `state/price_last.json`.
