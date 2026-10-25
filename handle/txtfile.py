import re
from io import BytesIO
from datetime import datetime, timezone

from pyrogram import filters

from config import TXTFILE_COOLDOWN, TXTFILE_MAX_LENGTH
from task_queue import enqueue

# Characters that are not allowed in file names.
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def make_filename(custom):
    """/txtfile my notes  ->  'my notes.txt' (safe, max 50 chars)."""
    name = _BAD_NAME_CHARS.sub("", custom or "").strip(" .")
    name = name[:50].strip(" .")
    if not name:
        name = "text_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return name + ".txt"


def register_txtfile(app):
    """/txtfile — turn a replied text into a downloadable .txt file."""

    @app.on_message(filters.command("txtfile"))
    async def txtfile_command(client, message):
        user = message.from_user
        if not user:
            return

        source = message.reply_to_message
        text = ((source.text or source.caption or "") if source else "").strip()

        if not text:
            await message.reply_text(
                "📝 Send a text message, then reply to it with /txtfile.\n"
                "Optional: /txtfile my_name to choose the file name."
            )
            return

        if len(text) > TXTFILE_MAX_LENGTH:
            await message.reply_text(
                f"⚠️ Text is too long! Please use {TXTFILE_MAX_LENGTH} characters or fewer."
            )
            return

        filename = make_filename(" ".join(message.command[1:]))
        data = text.encode("utf-8")

        async def job():
            buffer = BytesIO(data)
            buffer.name = filename
            try:
                await message.reply_document(
                    document=buffer,
                    file_name=filename,
                    caption=f"✅ Your text file is ready!\n📄 {filename}",
                )
            finally:
                buffer.close()

        await enqueue(
            message,
            user.id,
            job,
            kind="txtfile",
            command="/txtfile",
            timeout=30,
            cooldown=TXTFILE_COOLDOWN,
            error_text="❌ Couldn't create your text file. Please try again later.",
        )
