"""Create a Telethon StringSession for Render deployment.

Run this locally, not on Render:
    TG_API_ID=123 TG_API_HASH=... python telegram-groq-bot/create_session.py
"""

import os

from telethon import TelegramClient
from telethon.sessions import StringSession


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


api_id = int(required_env("TG_API_ID"))
api_hash = required_env("TG_API_HASH")

with TelegramClient(StringSession(), api_id, api_hash) as telegram:
    print("\nTELEGRAM_SESSION (copy this entire value into your secret store):\n")
    print(telegram.session.save())
    print()
