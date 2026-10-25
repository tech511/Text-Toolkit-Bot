import logging

from pyrogram import filters
from pyrogram.handlers import MessageHandler

from config import UNBAN_TEXT
from database import user_info
from handle.ban import is_owner, parse_user_id

log = logging.getLogger(__name__)


async def unban_command(client, message):
    user = message.from_user
    if not user:
        return

    if not is_owner(user):
        await message.reply_text("⛔ This command is only for the bot owner.")
        return

    target = parse_user_id(message)
    if target is None:
        await message.reply_text("Usage: /unban [user_id]\nExample: /unban 123456789")
        return

    try:
        if not await user_info.is_banned(target):
            await message.reply_text(f"ℹ️ User {target} isn't banned.")
            return
        await user_info.unban_user(target)
    except Exception:
        log.warning("unban failed", exc_info=True)
        await message.reply_text("❌ Database error. The user was NOT unbanned, try again.")
        return

    await message.reply_text(f"✅ User {target} has been unbanned.")

    # Best effort: tell the user they can use the bot again.
    try:
        await client.send_message(target, UNBAN_TEXT)
    except Exception:
        pass


def register_unban(app):
    app.add_handler(MessageHandler(unban_command, filters.command("unban")))
