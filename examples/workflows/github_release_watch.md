---
name: GitHub Release Watch
schedule: "20 8 * * 1-5"
description: Weekday mornings, notify when a watched repository publishes a new release.
---

1. `curl -s https://api.github.com/repos/example-org/example-tool/releases/latest`
2. Compare `tag_name` with the tag stored in `state/release_seen.txt`.
3. If it is new, send a Telegram message: "New release: example-tool <tag> <html_url>" and write the tag back to the state file.
