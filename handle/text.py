
import html
import time

from pyrogram import filters
from pyrogram.enums import ParseMode


MAX_TEXT_LENGTH = 4000
COOLDOWN = 2

last_request = {}


def register_text(app):

    @app.on_message(filters.command([
        "count", "upper", "lower", "reverse",
        "clean", "bold", "italic", "mono"
    ]))
    async def text_tool(client, message):

        user = message.from_user
        source = message.reply_to_message

        if not user:
            return

        if not source or not source.text:
            await message.reply_text(
                "📝 Send a text message, then reply to it "
                "with a text command."
            )
            return

        text = source.text

        if len(text) > MAX_TEXT_LENGTH:
            await message.reply_text(
                "⚠️ Text is too long! Please use "
                "4000 characters or fewer."
            )
            return

        user_id = user.id
        now = time.monotonic()
        elapsed = now - last_request.get(user_id, 0)

        if elapsed < COOLDOWN:
            remaining = max(1, int(COOLDOWN - elapsed + 0.99))
            await message.reply_text(
                f"⏳ Please wait {remaining} second(s) "
                "before using another text command."
            )
            return

        last_request[user_id] = now
        command = message.command[0].lower()

        try:
            if command == "count":
                result = (
                    f"📝 Words: {len(text.split())}\n"
                    f"🔠 Characters: {len(text)}"
                )

            elif command == "upper":
                result = text.upper()

            elif command == "lower":
                result = text.lower()

            elif command == "reverse":
                result = text[::-1]

            elif command == "clean":
                result = " ".join(text.split())

            elif command == "bold":
                result = f"<b>{html.escape(text)}</b>"

            elif command == "italic":
                result = f"<i>{html.escape(text)}</i>"

            elif command == "mono":
                result = f"<code>{html.escape(text)}</code>"

            else:
                return

            if command in ("bold", "italic", "mono"):
                await message.reply_text(
                    result,
                    parse_mode=ParseMode.HTML
                )
            else:
                await message.reply_text(result)

        except Exception:
            try:
                await message.reply_text(
                    "❌ Something went wrong. Please try again."
                )
            except Exception:
                pass
