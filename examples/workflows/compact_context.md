---
name: Session Context Hygiene
schedule: "50 */2 * * *"
description: Every 2 hours, compact the agent's own conversation context.
---

When idle, run `/compact` in this session to keep the context window small.
This only makes sense inside the running agent session.
