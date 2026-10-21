# Notification adapters

Every adapter implements `send(text) -> bool`. A failure is logged and returned as
`False`, never raised, so a broken channel can't crash a job. To add a channel, subclass
`aho.notify.Notifier` and add it to `ADAPTERS`. Check your setup with
`python -m aho test-notify`.

## Telegram (fully supported)

1. Create a bot with @BotFather and copy its token.
2. Send your bot any message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`.
3. Configure it:

```json
{"notify": {"channel": "telegram", "telegram_bot_token": "<token>", "telegram_chat_id": "<chat id>"}}
```

You can use the environment variables `AHO_TELEGRAM_BOT_TOKEN` and
`AHO_TELEGRAM_CHAT_ID` instead. Messages go through `POST /bot<token>/sendMessage`.
Messages longer than 4000 characters are split, because Telegram's limit is 4096.

## WhatsApp via Twilio (`whatsapp_twilio`)

This calls `POST https://api.twilio.com/2010-04-01/Accounts/<SID>/Messages.json` with
HTTP Basic auth and the fields `From=whatsapp:+<E.164>`, `To=whatsapp:+<E.164>` and
`Body`.

- WhatsApp only allows free-form messages within **24 hours of the user's last message**
  to your number. Alerts and digests are business-initiated, so they usually need an
  **approved template**. Set `twilio_content_sid` (`HX...`) and `aho` sends `ContentSid`
  with `ContentVariables={"1": "<report text>"}` instead of `Body`.
- For testing, use the Twilio WhatsApp Sandbox. Join it from your phone first.

Environment variables: `AHO_TWILIO_ACCOUNT_SID`, `AHO_TWILIO_AUTH_TOKEN`,
`AHO_TWILIO_FROM`, `AHO_TWILIO_TO` and `AHO_TWILIO_CONTENT_SID`.

## WhatsApp via CallMeBot (`whatsapp_callmebot`)

This is a free service for personal notifications to **your own** number. Activate it by
messaging the CallMeBot number as described on callmebot.com; it replies with an API key.
`aho` then calls
`GET https://api.callmebot.com/whatsapp.php?phone=<+number>&text=<text>&apikey=<key>`.
It is a third-party service, so check its terms and rate limits.

Environment variables: `AHO_CALLMEBOT_PHONE` and `AHO_CALLMEBOT_APIKEY`.

## Generic webhook (`webhook`)

This posts `{"text": "<message>"}` as JSON to `webhook_url` (or `AHO_WEBHOOK_URL`). Set
`webhook_field` to use a key other than `text`. Use it to connect any other gateway, for
example:

- a self-hosted WhatsApp bridge;
- an n8n or Make scenario;
- Home Assistant's notify service;
- a Discord or Slack relay;
- ntfy.

## Which messages are sent

| Message | When |
|---|---|
| **Failure alert** (`[aho] job 'x' FAILED ...`) | Sent by the runner immediately when a job exits non-zero (`alert_on_failure`). |
| **Job output** | Sent only for jobs run with `--notify-output`: their stdout on success, unless it matches `--quiet-if`. Stubs are registered with `--notify-output`, so whatever a stub prints becomes the message. |
| **Health alert** | Sent by `heartbeat-check --mode alert` when a job is MISSED, FAILED, NEVER or HUNG. An unchanged problem set is resent only after `realert_hours`. |
| **Daily digest** | Sent by `heartbeat-check --mode digest`: one line per job, always sent. |
