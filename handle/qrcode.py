import asyncio
from io import BytesIO

import qrcode
from pyrogram import filters

from config import QRCODE_COOLDOWN
from task_queue import enqueue


def _build_qr(text: str) -> BytesIO:
    """CPU work, runs in a thread so the bot stays responsive."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=4,
        border=2
    )
    qr.add_data(text)
    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white"
    ).convert("RGB")

    buffer = BytesIO()
    buffer.name = "qrcode.png"
    try:
        image.save(buffer, format="PNG")
        buffer.seek(0)
        return buffer
    finally:
        image.close()


def register_qrcode(app):

    @app.on_message(filters.command("qrcode"))
    async def qrcode_command(client, message):

        user = message.from_user
        if not user:
            return

        if not message.reply_to_message:
            await message.reply_text(
                "First send text or a URL, then reply to it with /qrcode."
            )
            return

        source = message.reply_to_message

        if not source.text:
            await message.reply_text(
                "Please reply to a text message or URL."
            )
            return

        text = source.text.strip()

        if not text or len(text) > 1200:
            await message.reply_text(
                "Text must contain 1–1200 characters."
            )
            return

        async def job():
            try:
                buffer = await asyncio.to_thread(_build_qr, text)
            except qrcode.exceptions.DataOverflowError:
                await message.reply_text(
                    "⚠️ This text is too long to fit in a QR code. "
                    "Please shorten it."
                )
                return

            try:
                await message.reply_photo(
                    photo=buffer,
                    caption="✅ Here's your QR code!"
                )
            finally:
                buffer.close()

        await enqueue(
            message,
            user.id,
            job,
            kind="qrcode",
            command="/qrcode",
            timeout=30,
            cooldown=QRCODE_COOLDOWN,
            error_text="❌ Couldn't create your QR code. Please try again later.",
        )
