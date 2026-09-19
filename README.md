# Telegram DM Groq Bot

This worker replies with Groq AI in one-to-one Telegram direct messages only.
It ignores groups, supergroups, channels, and non-text messages.

## Required values

Set these values in Render's environment variables:

- `TG_API_ID` — Telegram API ID from `my.telegram.org`
- `TG_API_HASH` — Telegram API hash from `my.telegram.org`
- `TELEGRAM_SESSION` — an authorized Telethon StringSession
- `OWNER_ID` — numeric Telegram ID allowed to control the userbot
- `GROQ_API_KEY` — a Groq Console API key

The API hash, session, and Groq key are secrets. Do not commit them to the
repository or paste them into source files.

## Create the Telegram session

Render workers do not have an interactive terminal for Telegram's first login.
Create the session once on a trusted local machine:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r telegram-groq-bot/requirements.txt

export TG_API_ID="your-api-id"
export TG_API_HASH="your-api-hash"
python telegram-groq-bot/create_session.py
```

Enter Telegram's phone number, login code, and two-factor password if asked.
Copy the printed `TELEGRAM_SESSION` value into Render. Treat it like a
password: anyone who has it can operate the Telegram session.

## Deploy on Render as a Web Service

1. Push this repository to GitHub or another Git provider.
2. In Render, choose **New → Blueprint** and select the repository.
3. Render will read `render.yaml` and create a Web Service.
4. Add the required environment variables, then deploy.
5. Check the service logs for `Telegram DM Groq web service started`.

The service listens on Render's `PORT` and exposes `/healthz` for Render
health checks. The HTTP endpoint is only for service health; Telegram messages
are still handled through Telethon.

The default AI personality is friendly and conversational. To customize its
tone, set `GROQ_SYSTEM_PROMPT` in Render.

## Userbot commands

Send these commands from the configured owner's private chat. You can also
send them from the logged-in account's Saved Messages when `OWNER_ID` is blank:

```text
/on
/start
/off
/stop
/status
/help
/setprompt You are a warm and playful conversation partner.
/resetprompt
```

Commands are handled only in private chats and are never sent to Groq. Normal
users receive AI replies only when auto-replies are enabled.

## Local run

```bash
pip install -r telegram-groq-bot/requirements.txt
TG_API_ID=... TG_API_HASH=... TELEGRAM_SESSION=... GROQ_API_KEY=... \
  python telegram-groq-bot/main.py
```