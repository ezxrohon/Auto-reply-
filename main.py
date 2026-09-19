"""Telegram DM auto-reply worker powered by Groq."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict, deque
from typing import Any

import httpx
from telethon import TelegramClient, events
from telethon.sessions import StringSession


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("telegram-groq-bot")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


API_ID = int(os.getenv("TG_API_ID", "309"))
API_HASH = required_env("TG_API_HASH")
TELEGRAM_SESSION = required_env("TELEGRAM_SESSION")
GROQ_API_KEY = required_env("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
SYSTEM_PROMPT = os.getenv(
    "GROQ_SYSTEM_PROMPT",
    (
        "You are a warm, friendly Telegram direct-message AI companion. "
        "Chat naturally and make the conversation feel relaxed, kind, and personal. "
        "Show genuine interest by asking thoughtful follow-up questions when appropriate. "
        "Keep replies conversational and not overly long. "
        "Use light humor when it fits, but never be rude, pushy, or judgmental. "
        "Do not claim to be human or pretend to have real-world experiences. "
        "If you do not know something, say so instead of inventing details."
    ),
)
OWNER_ID_VALUE = os.getenv("OWNER_ID", "").strip()
owner_user_id: int | None = int(OWNER_ID_VALUE) if OWNER_ID_VALUE else None
auto_reply_enabled = True
current_system_prompt = SYSTEM_PROMPT

# Keep a small in-memory conversation window per DM. It resets when Render
# restarts the worker, which avoids storing private message history on disk.
conversation_history: dict[int, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=10)
)
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)


async def health_response(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Serve the health endpoint required by a Render Web Service."""
    try:
        request = await asyncio.wait_for(reader.read(2048), timeout=5)
        lines = request.decode("latin-1", errors="replace").splitlines()
        request_line = lines[0] if lines else ""
        request_parts = request_line.split(" ")
        path = request_parts[1] if len(request_parts) > 1 else "/"

        if path == "/healthz" or path == "/":
            body = b'{"status":"ok","service":"telegram-groq-dm-bot"}'
            status = "200 OK"
        else:
            body = b'{"status":"not_found"}'
            status = "404 Not Found"

        headers = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, BrokenPipeError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def run_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(health_response, "0.0.0.0", port)
    logger.info("Health server listening on port %s", port)
    async with server:
        await server.serve_forever()


async def ask_groq(user_id: int, text: str) -> str:
    """Generate a response using only the current user's private-chat history."""
    history = list(conversation_history[user_id])
    messages = [{"role": "system", "content": current_system_prompt}, *history]
    messages.append({"role": "user", "content": text})

    payload: dict[str, Any] = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 500,
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=45.0) as http:
        response = await http.post(GROQ_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    reply = data["choices"][0]["message"]["content"].strip()
    if not reply:
        raise RuntimeError("Groq returned an empty response")

    conversation_history[user_id].append({"role": "user", "content": text})
    conversation_history[user_id].append({"role": "assistant", "content": reply})
    return reply


async def handle_control_command(
    event: events.NewMessage.Event, text: str
) -> bool:
    """Handle owner-only controls without sending the command to Groq."""
    global auto_reply_enabled, current_system_prompt

    if owner_user_id is None or event.sender_id != owner_user_id:
        return False

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in {"/on", "/start"}:
        auto_reply_enabled = True
        await event.respond("Friendly DM auto-replies are ON.")
        return True

    if command in {"/off", "/stop"}:
        auto_reply_enabled = False
        await event.respond("Friendly DM auto-replies are OFF.")
        return True

    if command == "/status":
        state = "ON" if auto_reply_enabled else "OFF"
        await event.respond(
            f"Auto-replies: {state}\n"
            f"Model: {GROQ_MODEL}\n"
            f"Owner ID: {owner_user_id}"
        )
        return True

    if command == "/help":
        await event.respond(
            "Userbot commands:\n"
            "/on or /start - enable DM auto-replies\n"
            "/off or /stop - disable DM auto-replies\n"
            "/status - show bot status\n"
            "/setprompt <text> - change the AI personality until restart\n"
            "/resetprompt - restore the default personality"
        )
        return True

    if command == "/setprompt":
        if not argument:
            await event.respond("Usage: /setprompt <new AI personality>")
            return True
        current_system_prompt = argument
        await event.respond("AI personality updated for this session.")
        return True

    if command == "/resetprompt":
        current_system_prompt = SYSTEM_PROMPT
        await event.respond("AI personality reset to the default friendly style.")
        return True

    return False


async def send_in_chunks(event: events.NewMessage.Event, text: str) -> None:
    """Telegram messages have a length limit; split longer AI replies safely."""
    max_length = 4000
    for start in range(0, len(text), max_length):
        await event.respond(text[start : start + max_length])


@client.on(events.NewMessage(incoming=True))
async def handle_message(event: events.NewMessage.Event) -> None:
    # This is the hard boundary that prevents replies in groups and channels.
    # Telethon reports one-to-one private conversations as event.is_private.
    if not event.is_private:
        return

    if not event.sender_id:
        return

    text = (event.raw_text or "").strip()
    if not text:
        return

    if await handle_control_command(event, text):
        return

    if not auto_reply_enabled:
        return

    user_id = event.sender_id
    async with user_locks[user_id]:
        try:
            reply = await ask_groq(user_id, text)
            await send_in_chunks(event, reply)
            logger.info("Sent Groq DM reply to user %s", user_id)
        except httpx.HTTPStatusError as error:
            logger.error(
                "Groq request failed with status %s: %s",
                error.response.status_code,
                error.response.text[:500],
            )
            await event.respond(
                "Sorry, I couldn't generate a reply right now. Please try again shortly."
            )
        except Exception:
            logger.exception("Failed to handle private message from user %s", user_id)
            await event.respond(
                "Sorry, I couldn't generate a reply right now. Please try again shortly."
            )


@client.on(events.NewMessage(outgoing=True))
async def handle_saved_message_commands(event: events.NewMessage.Event) -> None:
    """Allow the account owner to control the userbot from Saved Messages."""
    if (
        not event.is_private
        or owner_user_id is None
        or event.chat_id != owner_user_id
    ):
        return

    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        await handle_control_command(event, text)


async def main() -> None:
    global owner_user_id

    health_task = asyncio.create_task(run_health_server())
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "TELEGRAM_SESSION is not authorized. Generate a new StringSession "
                "with create_session.py and update the secret."
            )

        me = await client.get_me()
        if owner_user_id is None:
            owner_user_id = me.id
        logger.info(
            "Telegram DM Groq web service started as %s",
            getattr(me, "username", None) or getattr(me, "id", "unknown"),
        )
        logger.info("Owner command ID is %s", owner_user_id)
        await client.run_until_disconnected()
    finally:
        health_task.cancel()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())