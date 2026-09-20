"""Telegram DM auto-reply worker powered by Groq."""

from __future__ import annotations

import asyncio
import ast
import base64
import logging
import operator
import os
import random
import time
from collections import defaultdict, deque
from typing import Any

import httpx
from telethon import TelegramClient, events, functions, types
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
love_target_user: int | None = None
afk_active = False
afk_reason = "I am away right now. I will reply when I am back."
afk_notified_users: set[int] = set()
started_at = time.monotonic()

SHAYARI = [
    "💖 ᴛᴜᴍʜᴀʀɪ ᴇᴋ ᴍᴜsᴋᴀᴀɴ ʜɪ ᴅɪʟ ᴋᴏ ᴋʜᴜsʜ ᴋᴀʀɴᴇ ᴋᴇ ʟɪʏᴇ ᴋᴀᴀғɪ ʜᴀɪ ✨",
    "🌹 ᴋᴜᴄʜ ʟᴏɢ ᴢɪɴᴅᴀɢɪ ᴍᴇ ɴᴀʜɪ, ᴅɪʟ ᴍᴇ ʜᴀᴍᴇsʜᴀ ʀᴀʜᴛᴇ ʜᴀɪɴ 💕",
    "💫 ᴛᴜᴍʜᴀʀᴇ ᴍᴇssᴀɢᴇ ᴋᴀ ɪɴᴛᴇᴢᴀᴀʀ ʜᴀʀ ʙᴀᴀʀ ʀᴇʜᴛᴀ ʜᴀɪ 💗",
    "🦋 ᴅɪʟ ᴋᴇ ᴋᴀᴀʀɪʙ ᴡᴏʜɪ ʜᴏᴛᴇ ʜᴀɪɴ ᴊᴏ ᴋʜᴀᴀs ʜᴏᴛᴇ ʜᴀɪɴ 💖",
    "🌙 ᴛᴜᴍʜᴀʀᴀ ɴᴀᴀᴍ ᴀᴀᴛᴇ ʜɪ ᴄʜᴇʜʀᴇ ᴘᴀʀ ᴍᴜsᴋᴀᴀɴ ᴀᴀ ᴊᴀᴀᴛɪ ʜᴀɪ ✨",
    "🥀 ᴋᴜᴄʜ ᴍᴜʟᴀᴀᴋᴀᴀᴛᴇɴ ᴄʜʜᴏᴛɪ ʜᴏᴛɪ ʜᴀɪɴ, ᴘᴀʀ ʏᴀᴀᴅᴇɪɴ ʟᴀᴍʙɪ ʜᴏᴛɪ ʜᴀɪɴ ❤️",
    "💐 ᴛᴜᴍ ᴘᴀᴀs ʜᴏ ʏᴀ ᴅᴜᴜʀ, ᴅɪʟ ᴍᴇ ᴛᴜᴍʜᴀʀɪ ᴊᴀɢᴀʜ ʜᴀᴍᴇsʜᴀ ʜᴀɪ 💞",
    "⭐ ᴛᴜᴍʜᴀʀɪ ʜᴀʀ ʙᴀᴀᴛ ᴍᴇ ᴋᴜᴄʜ ᴋʜᴀᴀs sᴀ ᴀʜsᴀᴀs ʜᴏᴛᴀ ʜᴀɪ 💖",
]

# Keep a small in-memory conversation window per DM. It resets when Render
# restarts the worker, which avoids storing private message history on disk.
conversation_history: dict[int, deque[dict[str, str]]] = defaultdict(
    lambda: deque(maxlen=10)
)
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)

CALCULATOR_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
CALCULATOR_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def calculate_node(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return calculate_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            raise ValueError("boolean values are not allowed")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in CALCULATOR_OPERATORS:
        left = calculate_node(node.left)
        right = calculate_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError("exponent is too large")
        return CALCULATOR_OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in CALCULATOR_UNARY_OPERATORS:
        return CALCULATOR_UNARY_OPERATORS[type(node.op)](calculate_node(node.operand))
    raise ValueError("only basic arithmetic is supported")


def calculate(expression: str) -> int | float:
    if len(expression) > 120:
        raise ValueError("expression is too long")
    return calculate_node(ast.parse(expression, mode="eval"))


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
    global auto_reply_enabled, current_system_prompt, love_target_user
    global afk_active, afk_reason, afk_notified_users

    is_owner_message = event.sender_id == owner_user_id or event.out
    if owner_user_id is None or not is_owner_message:
        return False

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    if command in {"/ping", ".ping"}:
        await event.respond("🏓 Pong!")
        return True

    if command in {"/alive", ".alive"}:
        elapsed = int(time.monotonic() - started_at)
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        await event.respond(
            f"✅ Userbot is online\n⏱ Uptime: {hours}h {minutes}m {seconds}s"
        )
        return True

    if command in {"/id", ".id"}:
        await event.respond(
            f"🆔 Chat ID: {event.chat_id}\n"
            f"👤 Sender ID: {event.sender_id}"
        )
        return True

    if command in {"/info", ".info"}:
        chat = await event.get_chat()
        first_name = getattr(chat, "first_name", None) or getattr(
            chat, "title", None
        ) or "Private chat"
        username = getattr(chat, "username", None)
        username_text = f"@{username}" if username else "none"
        await event.respond(
            f"👤 Name: {first_name}\n"
            f"🔗 Username: {username_text}\n"
            f"🆔 ID: {event.chat_id}"
        )
        return True

    if command in {"/reverse", ".reverse"}:
        await event.respond(argument[::-1] if argument else "Usage: .reverse <text>")
        return True

    if command in {"/upper", ".upper"}:
        await event.respond(argument.upper() if argument else "Usage: .upper <text>")
        return True

    if command in {"/lower", ".lower"}:
        await event.respond(argument.lower() if argument else "Usage: .lower <text>")
        return True

    if command in {"/b64", ".b64"}:
        if not argument:
            await event.respond("Usage: .b64 <text>")
            return True
        encoded = base64.b64encode(argument.encode("utf-8")).decode("ascii")
        await event.respond(encoded)
        return True

    if command in {"/unb64", ".unb64"}:
        try:
            decoded = base64.b64decode(argument, validate=True).decode("utf-8")
            await event.respond(decoded)
        except Exception:
            await event.respond("Usage: .unb64 <valid base64 text>")
        return True

    if command in {"/calc", ".calc"}:
        if not argument:
            await event.respond("Usage: .calc <basic arithmetic>")
            return True
        try:
            await event.respond(f"🧮 {calculate(argument)}")
        except Exception:
            await event.respond(
                "❌ Only basic arithmetic is supported, for example: .calc 12 * (3 + 2)"
            )
        return True

    if command in {"/dice", ".dice"}:
        await event.respond(f"🎲 {random.randint(1, 6)}")
        return True

    if command in {"/flip", ".flip"}:
        await event.respond(f"🪙 {random.choice(['Heads', 'Tails'])}")
        return True

    if command in {"/8ball", ".8ball"}:
        await event.respond(
            random.choice(
                [
                    "🎱 Yes.",
                    "🎱 No.",
                    "🎱 Probably.",
                    "🎱 Ask me again later.",
                    "🎱 It looks promising.",
                ]
            )
        )
        return True

    if command in {"/afk", ".afk"}:
        afk_active = True
        afk_reason = argument or "I am away right now. I will reply when I am back."
        afk_notified_users.clear()
        await event.respond(f"🌙 AFK mode enabled.\nReason: {afk_reason}")
        return True

    if command in {"/stopafk", ".stopafk"}:
        afk_active = False
        afk_notified_users.clear()
        await event.respond("✅ AFK mode disabled.")
        return True

    if command == "/love":
        if not event.is_reply:
            await event.respond(
                "💖 Reply to a user's message and send /love to set the target."
            )
            return True

        replied = await event.get_reply_message()
        sender = await replied.get_sender() if replied else None
        if not sender or getattr(sender, "bot", False):
            await event.respond("❌ A real user message could not be found.")
            return True

        love_target_user = sender.id
        name = sender.first_name or "USER"
        await event.respond(
            f"💖 LOVE TARGET SET 💖\n\n"
            f"👤 {name}\n"
            "✨ Shayari will be sent on their private messages."
        )
        return True

    if command == "/song":
        if not love_target_user:
            await event.respond("❌ Set a target first by replying with /love.")
            return True
        if not argument:
            await event.respond("🎵 Usage: /song Song Name / Song Link")
            return True

        try:
            user = await client.get_entity(love_target_user)
            message = (
                f"🎵💖 {getattr(user, 'first_name', None) or 'USER'} 💖🎵\n\n"
                "✨ Ye song tumhare liye 🌙\n\n"
                f"🎶 {argument}"
            )
            if event.chat_id == love_target_user:
                await client.send_message(
                    love_target_user, message, reply_to=event.id
                )
            else:
                await client.send_message(love_target_user, message)
        except Exception:
            logger.exception("Failed to send song to love target")
            await event.respond("❌ Song send error. Check the target and try again.")
        return True

    if command == "/ai":
        action = argument.lower()
        if action == "on":
            auto_reply_enabled = True
            await event.respond("Friendly DM auto-replies are ON.")
        elif action == "off":
            auto_reply_enabled = False
            await event.respond("Friendly DM auto-replies are OFF.")
        else:
            await event.respond("Usage: /ai on or /ai off")
        return True

    if command == "/stop":
        love_target_user = None
        auto_reply_enabled = False
        afk_active = False
        afk_notified_users.clear()
        await event.respond("🛑 Love automation and AI auto-replies are OFF.")
        return True

    if command == "/status":
        state = "ON" if auto_reply_enabled else "OFF"
        love_state = str(love_target_user) if love_target_user else "none"
        afk_state = "ON" if afk_active else "OFF"
        await event.respond(
            f"Auto-replies: {state}\n"
            f"Model: {GROQ_MODEL}\n"
            f"Owner ID: {owner_user_id}\n"
            f"Love target: {love_state}\n"
            f"AFK: {afk_state}"
        )
        return True

    if command in {"/help", ".help"}:
        await event.respond(
            "Userbot commands:\n"
            "/ai on - enable DM auto-replies\n"
            "/ai off - disable AI auto-replies\n"
            "/love - reply to a user's message to start shayari automation\n"
            "/song <name or link> - send a song to the love target\n"
            "/stop - stop love automation and AI auto-replies\n"
            "/status - show bot status\n"
            "/setprompt <text> - change the AI personality until restart\n"
            "/resetprompt - restore the default personality\n\n"
            "Private utilities:\n"
            ".ping .alive .id .info .calc .reverse .upper .lower\n"
            ".b64 .unb64 .dice .flip .8ball\n"
            ".afk <reason> .stopafk"
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


async def send_random_pack_sticker(
    event: events.NewMessage.Event, message: Any
) -> bool:
    """Reply with a random sticker from the incoming sticker's pack."""
    document = getattr(message, "document", None)
    attributes = getattr(document, "attributes", ()) if document else ()
    sticker_set = next(
        (
            attribute.stickerset
            for attribute in attributes
            if isinstance(attribute, types.DocumentAttributeSticker)
        ),
        None,
    )
    if not sticker_set or isinstance(sticker_set, types.InputStickerSetEmpty):
        logger.info("Sticker has no identifiable pack; skipping reply")
        return False

    try:
        sticker_pack = await client(
            functions.messages.GetStickerSetRequest(
                stickerset=sticker_set,
                hash=0,
            )
        )
        stickers = list(getattr(sticker_pack, "documents", ()))
        if not stickers:
            logger.info("Sticker pack has no stickers; skipping reply")
            return False

        await event.respond(file=random.choice(stickers))
        logger.info("Sent a random sticker from the incoming pack")
        return True
    except Exception:
        logger.exception("Failed to load the incoming sticker pack")
        return False


@client.on(events.NewMessage(incoming=True))
async def handle_message(event: events.NewMessage.Event) -> None:
    # This is the hard boundary that prevents replies in groups and channels.
    # Telethon reports one-to-one private conversations as event.is_private.
    if not event.is_private:
        return

    if not event.sender_id:
        return

    text = (event.raw_text or "").strip()

    if text and await handle_control_command(event, text):
        return

    message = getattr(event, "message", None)

    if afk_active and event.sender_id != owner_user_id:
        if event.sender_id not in afk_notified_users:
            afk_notified_users.add(event.sender_id)
            await event.respond(f"🌙 AFK: {afk_reason}")
        return

    if getattr(message, "sticker", False):
        await send_random_pack_sticker(event, message)
        return

    # Ignore all other non-text messages. Automated text replies remain
    # intentionally limited to actual text messages.
    if not text:
        return

    # In a one-to-one dialog, chat_id is the target user's ID. Use it as the
    # primary match and keep sender_id as a fallback for Telethon event shapes.
    if love_target_user and (
        event.chat_id == love_target_user or event.sender_id == love_target_user
    ):
        sender = await event.get_sender()
        name = getattr(sender, "first_name", None) or "USER"
        logger.info("Sending love shayari to target user %s", love_target_user)
        await event.respond(f"💖 {name.upper()} 💖\n\n{random.choice(SHAYARI)}")
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
async def handle_outgoing_private_commands(
    event: events.NewMessage.Event,
) -> None:
    """Allow owner commands in Saved Messages or any private user chat."""
    if not event.is_private or owner_user_id is None:
        return

    text = (event.raw_text or "").strip()
    if text.startswith(("/", ".")):
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