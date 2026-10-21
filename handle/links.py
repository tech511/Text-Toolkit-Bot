"""Cloudinary image/video direct-link commands for Text-Toolkit-Bot (Pyrogram 2.x).

Commands:
  /imglink - reply to a photo or image document
  /vidlink - reply to a video or video document
  /tlink   - auto-detect image/video

Required environment variables:
  CLOUDINARY_CLOUD_NAME
  CLOUDINARY_API_KEY
  CLOUDINARY_API_SECRET

Optional:
  BOT_TOKEN (enables Telegram's Copy Text button where supported)
  CLOUDINARY_FOLDER (defaults to text-toolkit-bot)
"""

import asyncio
import json
import logging
import os
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import cloudinary
import cloudinary.uploader
from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

# Keep this limit modest for Render's memory/disk budget. Adjust if your
# Cloudinary plan and Render service can safely handle larger Telegram files.
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
UPLOAD_TIMEOUT = 120
CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "text-toolkit-bot")


def _configure_cloudinary() -> None:
    """Configure Cloudinary from Render environment variables."""
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
    api_key = os.getenv("CLOUDINARY_API_KEY", "").strip()
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "").strip()
    if not all((cloud_name, api_key, api_secret)):
        raise RuntimeError(
            "Cloudinary credentials missing: set CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET in Render Environment."
        )
    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )


def _cloudinary_upload(file_path: str, kind: str, filename: str) -> str:
    """Upload one local media file and return its public HTTPS delivery URL."""
    size = os.path.getsize(file_path)
    if size <= 0:
        raise RuntimeError("Downloaded media file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError("FILE_TOO_LARGE")

    _configure_cloudinary()
    resource_type = "image" if kind == "image" else "video"
    base_name = Path(filename or Path(file_path).name).stem
    # Cloudinary creates a unique public ID by default; use a safe folder only.
    result = cloudinary.uploader.upload(
        file_path,
        resource_type=resource_type,
        folder=CLOUDINARY_FOLDER,
        use_filename=True,
        unique_filename=True,
        timeout=UPLOAD_TIMEOUT,
    )
    url = result.get("secure_url")
    if not isinstance(url, str) or not url.startswith("https://res.cloudinary.com/"):
        raise RuntimeError("Cloudinary response did not contain a valid secure_url.")
    logger.info(
        "Cloudinary upload successful: kind=%s bytes=%s public_id=%s",
        kind, size, result.get("public_id", "unknown"),
    )
    return url


async def _upload_file(file_path: str, kind: str, filename: str) -> str:
    return await asyncio.to_thread(_cloudinary_upload, file_path, kind, filename)


def _get_media(source, expected: str):
    """Return (kind, media object, suggested filename), or None."""
    if not source:
        return None

    if expected in ("image", "auto"):
        if source.photo:
            return "image", source.photo, "image.jpg"
        doc = source.document
        if doc and (doc.mime_type or "").lower().startswith("image/"):
            ext = Path(doc.file_name or "image.jpg").suffix or ".jpg"
            return "image", doc, f"image{ext}"

    if expected in ("video", "auto"):
        if source.video:
            ext = Path(source.video.file_name or "video.mp4").suffix or ".mp4"
            return "video", source.video, f"video{ext}"
        doc = source.document
        if doc and (doc.mime_type or "").lower().startswith("video/"):
            ext = Path(doc.file_name or "video.mp4").suffix or ".mp4"
            return "video", doc, f"video{ext}"

    return None


async def _add_copy_button(client, sent_message, url: str) -> None:
    """Add a Telegram Copy Text button when possible; otherwise a URL button."""
    token = os.getenv("BOT_TOKEN", "").strip()
    if token:
        markup = {
            "inline_keyboard": [[
                {"text": "📋 Copy Link", "copy_text": {"text": url}}
            ]]
        }
        api_url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
        body = urllib.parse.urlencode({
            "reply_markup": json.dumps(markup, ensure_ascii=False),
            "chat_id": sent_message.chat.id,
            "message_id": sent_message.id,
        }).encode()
        request = urllib.request.Request(
            api_url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            def call_api():
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.read().decode("utf-8", errors="replace")
            result = await asyncio.to_thread(call_api)
            if '"ok":true' in result.replace(" ", ""):
                return
            logger.warning("Telegram Copy Text button was rejected; using URL button.")
        except Exception:
            logger.exception("Could not add Telegram Copy Text button; using URL button.")

    await sent_message.edit_reply_markup(
        InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Direct Link", url=url)]])
    )


async def _handle_link_command(client, message, expected: str):
    source = message.reply_to_message
    command = {"image": "/imglink", "video": "/vidlink", "auto": "/tlink"}[expected]
    if not source:
        await message.reply_text(
            "📎 আগে একটি ছবি/ভিডিও পাঠাও, তারপর সেটির reply করে "
            f"{command} লেখো।"
        )
        return

    media = _get_media(source, expected)
    if not media:
        hint = {
            "image": "🖼️ একটি ছবি বা image file-এ reply করে /imglink লেখো।",
            "video": "🎬 একটি ভিডিও বা video file-এ reply করে /vidlink লেখো।",
            "auto": "🖼️ ছবি বা 🎬 ভিডিওতে reply করে /tlink লেখো।",
        }[expected]
        await message.reply_text(hint)
        return

    kind, media_obj, fallback_name = media
    status = await message.reply_text("⏳ Media download হচ্ছে...")
    temp_path = None
    try:
        original_name = getattr(media_obj, "file_name", None) or fallback_name
        suffix = Path(original_name).suffix
        if not suffix or len(suffix) > 10:
            suffix = ".mp4" if kind == "video" else ".jpg"
        with tempfile.NamedTemporaryFile(prefix="tt_link_", suffix=suffix, delete=False) as temp:
            temp_path = temp.name

        downloaded = await client.download_media(source, file_name=temp_path)
        if not downloaded or not os.path.isfile(downloaded):
            raise RuntimeError("Telegram media download failed.")
        size = os.path.getsize(downloaded)
        if size <= 0:
            raise RuntimeError("Telegram downloaded an empty file.")
        if size > MAX_UPLOAD_BYTES:
            raise ValueError("FILE_TOO_LARGE")

        await status.edit_text("📤 Cloudinary-তে upload হচ্ছে...")
        url = await _upload_file(downloaded, kind, original_name)
        caption = (
            "✅ **Direct link তৈরি হয়েছে!**\n\n"
            f"🔗 `{url}`\n\n"
            "📋 নিচের button ব্যবহার করে লিংক কপি বা খুলতে পারো।"
        )
        if kind == "image":
            sent = await message.reply_photo(
                photo=downloaded, caption=caption, reply_to_message_id=source.id
            )
        else:
            sent = await message.reply_video(
                video=downloaded, caption=caption, reply_to_message_id=source.id
            )
        try:
            await _add_copy_button(client, sent, url)
        except Exception:
            logger.exception("Could not add link button to result message.")
            # The URL is already present in the caption, so the result remains usable.

    except ValueError as exc:
        if str(exc) == "FILE_TOO_LARGE":
            await message.reply_text("⚠️ ফাইলটি খুব বড়। এই ফিচারের সর্বোচ্চ সাইজ 30 MB।")
        else:
            logger.exception("Media link validation failed.")
            await message.reply_text("❌ ফাইলটি upload করা যায়নি। Render Logs দেখো।")
    except Exception:
        logger.exception("Cloudinary media-link upload failed.")
        await message.reply_text(
            "❌ Media upload ব্যর্থ হয়েছে। Render Logs-এ আসল error দেখো; "
            "Cloudinary credentials ও account limits-ও পরীক্ষা করো।"
        )
    finally:
        try:
            await status.delete()
        except Exception:
            pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                logger.warning("Could not remove temporary media file: %s", temp_path)


def register_links(app):
    """Register /imglink, /vidlink and /tlink commands on a Pyrogram Client."""
    @app.on_message(filters.command("imglink"))
    async def image_link_command(client, message):
        await _handle_link_command(client, message, "image")

    @app.on_message(filters.command("vidlink"))
    async def video_link_command(client, message):
        await _handle_link_command(client, message, "video")

    @app.on_message(filters.command("tlink"))
    async def auto_link_command(client, message):
        await _handle_link_command(client, message, "auto")
