import asyncio
import os
import sys
import tempfile
import time

from pyrogram import filters
from pyrogram.errors import RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    MAX_TEXT_LENGTH,
    COOLDOWN,
    MAX_RUNNING,
    MAX_WAITING,
    TTS_TIMEOUT,
)

# Voice presets use real Microsoft Edge neural voices.
# Pitch/rate changes are applied in addition to selecting a voice.
VOICE_PRESETS = {
    "deep_male": {
        "label": "🎙️ Deep Male",
        "voice": "en-US-GuyNeural",
        "rate": "-5%",
        "pitch": "-12Hz",
    },
    "female": {
        "label": "👩 Female",
        "voice": "en-US-AriaNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
    },
    "calm": {
        "label": "🌙 Soft / Calm",
        "voice": "en-US-JennyNeural",
        "rate": "-12%",
        "pitch": "-2Hz",
    },
    "energetic": {
        "label": "⚡ Fast / Energetic",
        "voice": "en-US-DavisNeural",
        "rate": "+12%",
        "pitch": "+0Hz",
    },
}

VOICE_SELECTION_TIMEOUT = max(30, min(int(TTS_TIMEOUT), 120))

queue = asyncio.Queue(maxsize=MAX_WAITING)
workers = []
active_users = set()
last_request = {}
pending_requests = {}


class TTSGenerationTimeout(Exception):
    """Raised when the Edge TTS subprocess exceeds its time limit."""


def voice_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    VOICE_PRESETS["deep_male"]["label"],
                    callback_data="ttsvoice:deep_male",
                ),
                InlineKeyboardButton(
                    VOICE_PRESETS["female"]["label"],
                    callback_data="ttsvoice:female",
                ),
            ],
            [
                InlineKeyboardButton(
                    VOICE_PRESETS["calm"]["label"],
                    callback_data="ttsvoice:calm",
                ),
                InlineKeyboardButton(
                    VOICE_PRESETS["energetic"]["label"],
                    callback_data="ttsvoice:energetic",
                ),
            ],
            [
                InlineKeyboardButton(
                    "✖️ Cancel",
                    callback_data="ttscancel",
                )
            ],
        ]
    )


async def create_audio_file(text, voice_key):
    """Generate audio in a killable subprocess so a timeout frees the worker."""
    preset = VOICE_PRESETS[voice_key]
    path = None
    process = None
    succeeded = False

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp:
            path = temp.name

        # A subprocess is intentional: cancelling a Python thread cannot stop
        # a blocked gTTS call, but this process can be terminated on timeout.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "edge_tts",
            "--voice",
            preset["voice"],
            f'--rate={preset["rate"]}',
            f'--pitch={preset["pitch"]}',
            "--text",
            text,
            "--write-media",
            path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=max(1, float(TTS_TIMEOUT)),
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            try:
                await process.communicate()
            except Exception:
                await process.wait()
            raise TTSGenerationTimeout from exc

        if process.returncode != 0:
            details = (stderr or b"").decode("utf-8", errors="replace").strip()
            # Keep logs/errors bounded; don't send raw provider errors to users.
            raise RuntimeError(details[-500:] or "Edge TTS generation failed.")

        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise RuntimeError("Edge TTS returned an empty audio file.")

        succeeded = True
        return path

    finally:
        # Also terminate the child if this coroutine is cancelled externally.
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await process.wait()
            except Exception:
                pass

        if not succeeded and path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


async def safe_reply(message, text):
    """A Telegram reply failure must not kill a worker."""
    try:
        await message.reply_text(text)
    except Exception:
        pass


async def remove_status(status):
    """Delete the status message without interrupting cleanup."""
    if status is not None:
        try:
            await status.delete()
        except Exception:
            pass


async def expire_voice_selection(user_id, selector_message_id):
    """Release a user's pending slot if they never choose a voice."""
    await asyncio.sleep(VOICE_SELECTION_TIMEOUT)
    pending = pending_requests.get(user_id)
    if pending and pending["selector_message_id"] == selector_message_id:
        pending_requests.pop(user_id, None)
        active_users.discard(user_id)
        try:
            await pending["selector_message"].edit_text(
                "⌛ Voice selection expired. Reply to a text with /tts to try again."
            )
        except Exception:
            pass


async def process_jobs():
    """Process queued jobs; a timed-out subprocess is killed before continuing."""
    while True:
        client, message, text, user_id, voice_key = await queue.get()

        audio_path = None
        status = None

        try:
            try:
                status = await message.reply_text("🔊 Generating audio...")
            except Exception:
                status = None

            try:
                audio_path = await create_audio_file(text, voice_key)
            except TTSGenerationTimeout:
                await safe_reply(
                    message,
                    "⏳ Speech generation timed out. The task was stopped; "
                    "you can try again shortly.",
                )
                continue

            if status is not None:
                try:
                    await status.edit_text("📤 Audio generated. Uploading...")
                except Exception:
                    pass

            preset = VOICE_PRESETS[voice_key]
            await message.reply_audio(
                audio=audio_path,
                caption=f"🔊 Your speech is ready!\n🎙️ Voice: {preset['label']}",
            )

        except RPCError:
            # Telegram errors must not stop the worker loop.
            pass

        except Exception:
            await safe_reply(
                message,
                "❌ Couldn't generate or send the speech. "
                "Please try again later.",
            )

        finally:
            await remove_status(status)

            if audio_path and isinstance(audio_path, str) and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

            active_users.discard(user_id)
            queue.task_done()


def register_tts(app):
    """Register /tts, voice-selection callbacks, and configured workers."""

    @app.on_message(filters.command("tts"))
    async def tts_command(client, message):
        user = message.from_user
        if not user:
            return

        user_id = user.id
        source = message.reply_to_message

        if not source or not source.text:
            await safe_reply(
                message,
                "📝 Send a text message, then reply to it with /tts.",
            )
            return

        text = source.text.strip()
        if not text:
            await safe_reply(message, "Please reply to a text message.")
            return

        if len(text) > MAX_TEXT_LENGTH:
            await safe_reply(
                message,
                f"⚠️ Please keep your text under {MAX_TEXT_LENGTH} characters.",
            )
            return

        if user_id in active_users or user_id in pending_requests:
            await safe_reply(
                message,
                "⏳ You already have a TTS task running, waiting, or selecting a voice.",
            )
            return

        now = time.monotonic()
        elapsed = now - last_request.get(user_id, 0)
        if elapsed < COOLDOWN:
            remaining = int(COOLDOWN - elapsed) + 1
            await safe_reply(
                message,
                f"⏳ Please wait {remaining} seconds before another request.",
            )
            return

        capacity = MAX_RUNNING + MAX_WAITING
        if len(active_users) >= capacity:
            await safe_reply(
                message,
                f"🚦 TTS queue is full ({capacity}/{capacity}). "
                "Please wait and try again.",
            )
            return

        if not workers:
            for _ in range(MAX_RUNNING):
                workers.append(asyncio.create_task(process_jobs()))

        try:
            selector_message = await message.reply_text(
                "🎙️ Choose the voice for your speech:",
                reply_markup=voice_keyboard(),
            )
        except Exception:
            await safe_reply(message, "❌ Couldn't show the voice selection menu.")
            return

        active_users.add(user_id)
        pending_requests[user_id] = {
            "client": client,
            "command_message": message,
            "text": text,
            "selector_message": selector_message,
            "selector_message_id": selector_message.id,
        }
        asyncio.create_task(
            expire_voice_selection(user_id, selector_message.id)
        )

    @app.on_callback_query(filters.regex(r"^ttsvoice:(deep_male|female|calm|energetic)$"))
    async def tts_voice_callback(client, callback_query):
        user = callback_query.from_user
        if not user:
            await callback_query.answer("Unable to identify you.", show_alert=True)
            return

        user_id = user.id
        pending = pending_requests.get(user_id)

        if (
            not pending
            or not callback_query.message
            or callback_query.message.id != pending["selector_message_id"]
        ):
            await callback_query.answer(
                "This voice menu has expired. Send /tts again.",
                show_alert=True,
            )
            return

        voice_key = callback_query.data.split(":", 1)[1]
        pending_requests.pop(user_id, None)

        try:
            queue.put_nowait(
                (
                    pending["client"],
                    pending["command_message"],
                    pending["text"],
                    user_id,
                    voice_key,
                )
            )
            last_request[user_id] = time.monotonic()
        except asyncio.QueueFull:
            active_users.discard(user_id)
            await callback_query.answer("The queue is full. Please try again.", show_alert=True)
            try:
                await callback_query.message.edit_text(
                    "🚦 TTS queue is full. Please reply to your text with /tts again later."
                )
            except Exception:
                pass
            return

        await callback_query.answer("Voice selected!")
        try:
            await callback_query.message.edit_text(
                f"✅ Request added to the queue.\n"
                f"🎙️ Voice: {VOICE_PRESETS[voice_key]['label']}\n"
                f"📋 Tasks in system: {len(active_users)}/{MAX_RUNNING + MAX_WAITING}"
            )
        except Exception:
            pass

    @app.on_callback_query(filters.regex(r"^ttscancel$"))
    async def tts_cancel_callback(client, callback_query):
        user = callback_query.from_user
        pending = pending_requests.get(user.id) if user else None

        if (
            not pending
            or not callback_query.message
            or callback_query.message.id != pending["selector_message_id"]
        ):
            await callback_query.answer("This menu has already expired.")
            return

        pending_requests.pop(user.id, None)
        active_users.discard(user.id)
        await callback_query.answer("TTS request cancelled.")
        try:
            await callback_query.message.edit_text("✖️ TTS request cancelled.")
        except Exception:
            pass
