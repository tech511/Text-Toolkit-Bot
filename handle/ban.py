"""/ban command + the global "banned users are blocked" gate.

The gate runs in handler group -2 (before everything else), so a banned
user cannot use ANY command — not even /start — and none of the other
handler files had to be changed.
"""

import logging
import time

from pyrogram import StopPropagation, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BAN_TEXT, OWNER_ID, OWNER_URL, OWNER_USERNAME
from database import user_info

log = logging.getLogger(__name__)

NOTICE_INTERVAL = 30  # seconds between two ban notices to the same user
_last_notice = {}


def ban_notice_markup():
    """Button that opens the owner's chat."""
    label = f"👤 @{OWNER_USERNAME}" if OWNER_USERNAME else "• ᴏᴡɴᴇʀ •"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=OWNER_URL)]])


def is_owner(user) -> bool:
    return bool(user and OWNER_ID and user.id == OWNER_ID)


# ───────────────────────── gate (group -2) ─────────────────────────
def _should_notice(user_id: int) -> bool:
    now = time.monotonic()
    if now - _last_notice.get(user_id, 0) < NOTICE_INTERVAL:
        return False
    _last_notice[user_id] = now
    if len(_last_notice) > 5000:
        for uid, seen in list(_last_notice.items()):
            if now - seen > NOTICE_INTERVAL:
                _last_notice.pop(uid, None)
    return True


async def _ban_gate_message(client, message):
    user = message.from_user
    if not user or is_owner(user):
        return  # continue to the next handler group

    try:
        banned = await user_info.is_banned_fast(user.id)
    except Exception:
        log.warning("ban check failed", exc_info=True)
        return

    if not banned:
        return

    is_command = bool(message.text and message.text.startswith("/"))
    private = bool(message.chat and message.chat.type.name == "PRIVATE")

    if (private or is_command) and _should_notice(user.id):
        try:
            await message.reply_text(BAN_TEXT, reply_markup=ban_notice_markup())
        except Exception:
            pass

    raise StopPropagation


async def _ban_gate_callback(client, query):
    user = query.from_user
    if not user or is_owner(user):
        return

    try:
        banned = await user_info.is_banned_fast(user.id)
    except Exception:
        log.warning("ban check failed", exc_info=True)
        return

    if not banned:
        return

    try:
        await query.answer(BAN_TEXT, show_alert=True)
    except Exception:
        pass

    raise StopPropagation


# ───────────────────────── /ban ─────────────────────────
def parse_user_id(message):
    """Return the id from '/cmd 12345' or None if missing/invalid."""
    args = message.command[1:]
    if len(args) != 1:
        return None
    raw = args[0]
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 15:
        return None
    value = int(raw)
    return value if value > 0 else None


async def ban_command(client, message):
    user = message.from_user
    if not user:
        return

    if not is_owner(user):
        await message.reply_text("⛔ This command is only for the bot owner.")
        return

    target = parse_user_id(message)
    if target is None:
        await message.reply_text("Usage: /ban [user_id]\nExample: /ban 123456789")
        return

    if target == user.id or is_owner_id(target):
        await message.reply_text("⚠️ You can't ban the owner.")
        return

    try:
        if await user_info.is_banned(target):
            await message.reply_text(f"ℹ️ User {target} is already banned.")
            return
        await user_info.ban_user(target, user.id)
    except Exception:
        log.warning("ban failed", exc_info=True)
        await message.reply_text("❌ Database error. The user was NOT banned, try again.")
        return

    await message.reply_text(f"🚫 User {target} has been banned.")

    # Best effort: tell the user (fails if they never started the bot).
    try:
        await client.send_message(
            target, BAN_TEXT, reply_markup=ban_notice_markup()
        )
    except Exception:
        pass


def is_owner_id(user_id: int) -> bool:
    return bool(OWNER_ID and user_id == OWNER_ID)


def register_ban(app):
    # Ban gate: blocks every update from banned users.
    app.add_handler(MessageHandler(_ban_gate_message, filters.all), group=-2)
    app.add_handler(CallbackQueryHandler(_ban_gate_callback), group=-2)

    app.add_handler(MessageHandler(ban_command, filters.command("ban")))
