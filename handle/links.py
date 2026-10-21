"""Image/video direct-link commands for Text-Toolkit-Bot (Pyrogram 2.x).

Commands:
  /imglink - reply to an image/photo
  /vidlink - reply to a video
  /tlink   - optional auto-detect alias for either media type

Files are temporarily downloaded to disk, uploaded anonymously to Catbox.moe,
and removed after the bot re-sends the media.
"""

import asyncio
import http.client
import json
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

HOST_URL = "https://catbox.moe/user/api.php"
MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # Below Catbox's advertised 200 MB cap.
UPLOAD_TIMEOUT = 120
logger = logging.getLogger(__name__)


def _catbox_upload(file_path: str, filename: str) -> str:
    """Stream a local file to Catbox without loading the whole file into RAM."""
    boundary = "----TextToolkitBotBoundary7MA4YWxkTrZu0gW"
    safe_name = os.path.basename(filename or "media.bin")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name)[:120] or "media.bin"
    size = os.path.getsize(file_path)
    if size > MAX_UPLOAD_BYTES:
        raise ValueError("FILE_TOO_LARGE")

    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="reqtype"\r\n\r\n',
        b"fileupload\r\n",
    ]
    # Anonymous uploads normally work. If Catbox rejects anonymous uploads
    # from the hosting IP, an optional account userhash can be set in Render.
    userhash = os.getenv("CATBOX_USERHASH", "").strip()
    if userhash:
        parts.extend([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="userhash"\r\n\r\n',
            userhash.encode(),
            b"\r\n",
        ])
    parts.extend([
        f"--{boundary}\r\n".encode(),
        (f'Content-Disposition: form-data; name="fileToUpload"; filename="{safe_name}"\r\n').encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
    ])
    prefix = b"".join(parts)
    suffix = f"\r\n--{boundary}--\r\n".encode()
    content_length = len(prefix) + size + len(suffix)

    connection = http.client.HTTPSConnection("catbox.moe", timeout=UPLOAD_TIMEOUT)
    try:
        connection.putrequest("POST", "/user/api.php")
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(content_length))
        connection.putheader("User-Agent", "Text-Toolkit-Bot/1.0")
        connection.endheaders()
        connection.send(prefix)
        with open(file_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 256)
                if not chunk:
                    break
                connection.send(chunk)
        connection.send(suffix)
        response = connection.getresponse()
        result = response.read(4096).decode("utf-8", errors="replace").strip()
        if response.status != 200:
            raise RuntimeError(f"Catbox HTTP {response.status}: {result[:300]}")
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Catbox request failed: {type(exc).__name__}: {exc}") from exc
    finally:
        connection.close()

    if not result.startswith("https://files.catbox.moe/"):
        raise RuntimeError(f"Catbox rejected upload: {result[:300] or 'empty response'}")
    return result


async def _upload_file(file_path: str, filename: str) -> str:
    return await asyncio.to_thread(_catbox_upload, file_path, filename)


def _get_media(source, expected: str):
    """Return (kind, media object, suggested filename), or None."""
    if not source:
        return None

    if expected in ("image", "auto"):
        if source.photo:
            return "image", source.photo, "image.jpg"
        doc = source.document
        if doc and (doc.mime_type or "").lower().startswith("image/"):
            ext = os.path.splitext(doc.file_name or "")[1] or ".img"
            return "image", doc, f"image{ext}"

    if expected in ("video", "auto"):
        if source.video:
            ext = os.path.splitext(source.video.file_name or "")[1] or ".mp4"
            return "video", source.video, f"video{ext}"
        doc = source.document
        if doc and (doc.mime_type or "").lower().startswith("video/"):
            ext = os.path.splitext(doc.file_name or "")[1] or ".mp4"
            return "video", doc, f"video{ext}"

    return None


async def _edit_with_copy_button(client, sent_message, url: str):
    """Use Bot API copy_text button where supported, else show a URL button."""
    token = getattr(client, "bot_token", None)
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
        except Exception:
            pass

    # Fallback for older Telegram Bot API/client combinations.
    try:
        await sent_message.edit_reply_markup(
            InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Direct Link", url=url)]])
        )
    except Exception:
        pass


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
        if expected == "image":
            hint = "🖼️ একটি ছবি বা image file-এ reply করে /imglink লেখো।"
        elif expected == "video":
            hint = "🎬 একটি ভিডিও বা video file-এ reply করে /vidlink লেখো।"
        else:
            hint = "🖼️ ছবি বা 🎬 ভিডিওতে reply করে /tlink লেখো।"
        await message.reply_text(hint)
        return

    kind, media_obj, fallback_name = media
    status = await message.reply_text("⏳ Media download হচ্ছে...")
    temp_path = None

    try:
        original_name = getattr(media_obj, "file_name", None) or fallback_name
        suffix = os.path.splitext(original_name)[1]
        if not suffix or len(suffix) > 10:
            suffix = ".mp4" if kind == "video" else ".jpg"
        with tempfile.NamedTemporaryFile(prefix="tt_link_", suffix=suffix, delete=False) as temp:
            temp_path = temp.name

        downloaded = await client.download_media(source, file_name=temp_path)
        if not downloaded or not os.path.isfile(downloaded):
            raise RuntimeError("DOWNLOAD_FAILED")
        size = os.path.getsize(downloaded)
        if size <= 0:
            raise RuntimeError("DOWNLOAD_FAILED")
        if size > MAX_UPLOAD_BYTES:
            raise ValueError("FILE_TOO_LARGE")

        try:
            await status.edit_text("📤 Hosting service-এ upload হচ্ছে...")
        except Exception:
            pass

        url = await _upload_file(downloaded, original_name)
        caption = (
            "✅ **Direct link তৈরি হয়েছে!**\n\n"
            f"🔗 `{url}`\n\n"
            "📋 নিচের Copy Link button চাপলে লিংক কপি হবে।"
        )
        if kind == "image":
            sent = await message.reply_photo(
                photo=downloaded, caption=caption, reply_to_message_id=source.id
            )
        else:
            sent = await message.reply_video(
                video=downloaded, caption=caption, reply_to_message_id=source.id
            )
        await _edit_with_copy_button(client, sent, url)

    except ValueError as exc:
        if str(exc) == "FILE_TOO_LARGE":
            await message.reply_text("⚠️ ফাইলটি খুব বড়। এই ফিচারের সর্বোচ্চ সাইজ 30 MB।")
        else:
            logger.exception("Unexpected value error in media-link handler")
            await message.reply_text("❌ ফাইলটি upload করা যায়নি। Render Logs দেখো।")
    except Exception as exc:
        # Keep the user message simple, but log the real cause in Render.
        logger.exception("Media-link upload failed: %s", exc)
        await message.reply_text(
            "❌ Media upload ব্যর্থ হয়েছে। Render Dashboard → Logs-এ "
            "এই error-এর বিস্তারিত পাওয়া যাবে।"
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
                pass


def register_links(app):
    """Register /imglink, /vidlink and /tlink (auto-detect alias)."""
    @app.on_message(filters.command("imglink"))
    async def image_link_command(client, message):
        await _handle_link_command(client, message, "image")

    @app.on_message(filters.command("vidlink"))
    async def video_link_command(client, message):
        await _handle_link_command(client, message, "video")

    @app.on_message(filters.command("tlink"))
    async def auto_link_command(client, message):
        await _handle_link_command(client, message, "auto")

