---
name: Blog Feed Watcher
schedule: "45 7,19 * * *"
description: Twice a day, check a blog's RSS feed for new posts and send a short summary of each new one.
---

1. Fetch the feed: `curl -s https://blog.example.com/feed.xml`
2. Compare item GUIDs with `state/feed_seen.json`; keep only new items.
3. If there are no new items, stop silently.
4. For each new item, summarise the post in 2-3 sentences.
5. Send one Telegram message with the titles, links and summaries, then update the state file.
