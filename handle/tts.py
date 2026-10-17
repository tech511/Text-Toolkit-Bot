
import asyncio
import os
import tempfile
import time

from gtts import gTTS
from pyrogram import filters
from pyrogram.errors import RPCError

from config import (
    MAX_TEXT_LENGTH,
    COOLDOWN,
    MAX_RUNNING,
    MAX_WAITING,
    TTS_TIMEOUT
)

queue = asyncio.Queue(maxsize=MAX_WAITING)
workers = []
active_users = set()
last_request = {}


def create_audio_file(text):
    """Create an MP3 and clean it up if generation fails."""
    path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False
        ) as temp:
            path = temp.name

        gTTS(
            text=text,
            lang="en",
            timeout=(5, 15)
        ).save(path)

        return path

    except Exception:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise


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


async def process_jobs():
    """Process queued jobs without animation tasks."""
    while True:
        client, message, text, user_id = await queue.get()

        audio_path = None
        generation_task = None
        status = None

        try:
            try:
                status = await message.reply_text(
                    "🔊 Generating audio..."
                )
            except Exception:
                status = None

            generation_task = asyncio.create_task(
                asyncio.to_thread(create_audio_file, text)
            )

            done, pending = await asyncio.wait(
                {generation_task},
                timeout=TTS_TIMEOUT
            )

            if pending:
                await safe_reply(
                    message,
                    "⏳ Speech generation timed out. "
                    "Please try again shortly."
                )

                # Keep this worker occupied until generation ends.
                # This prevents timed-out threads from piling up.
                try:
                    audio_path = await generation_task
                except Exception:
                    audio_path = None

            else:
                audio_path = generation_task.result()

                if status is not None:
                    try:
                        await status.edit_text(
                            "📤 Audio generated. Uploading..."
                        )
                    except Exception:
                        pass

                await message.reply_audio(
                    audio=audio_path,
                    caption="🔊 Your English speech is ready!"
                )

        except RPCError:
            # Telegram errors must not stop the worker loop.
            pass

        except Exception:
            await safe_reply(
                message,
                "❌ Couldn't generate or send the speech. "
                "Please try again later."
            )

        finally:
            await remove_status(status)

            if (
                audio_path
                and isinstance(audio_path, str)
                and os.path.exists(audio_path)
            ):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

            active_users.discard(user_id)
            queue.task_done()


def register_tts(app):
    """Register the TTS command and start the configured workers."""

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
                "📝 Send a text message, then reply to it with /tts."
            )
            return

        text = source.text.strip()

        if not text:
            await safe_reply(
                message,
                "Please reply to a text message."
            )
            return

        if len(text) > MAX_TEXT_LENGTH:
            await safe_reply(
                message,
                f"⚠️ Please keep your text under "
                f"{MAX_TEXT_LENGTH} characters."
            )
            return

        if user_id in active_users:
            await safe_reply(
                message,
                "⏳ You already have a TTS task running or waiting."
            )
            return

        now = time.monotonic()
        elapsed = now - last_request.get(user_id, 0)

        if elapsed < COOLDOWN:
            remaining = int(COOLDOWN - elapsed) + 1
            await safe_reply(
                message,
                f"⏳ Please wait {remaining} seconds "
                "before another request."
            )
            return

        if len(active_users) >= MAX_RUNNING + MAX_WAITING:
            await safe_reply(
                message,
                "🚦 TTS queue is full (5/5). "
                "Please wait and try again."
            )
            return

        if not workers:
            for _ in range(MAX_RUNNING):
                workers.append(
                    asyncio.create_task(process_jobs())
                )

        active_users.add(user_id)

        try:
            queue.put_nowait(
                (client, message, text, user_id)
            )
            last_request[user_id] = now

        except asyncio.QueueFull:
            active_users.discard(user_id)
            await safe_reply(
                message,
                "🚦 TTS queue is full. Please try again shortly."
            )
            return

        await safe_reply(
            message,
            f"✅ TTS request accepted. "
            f"Tasks in system: {len(active_users)}/5."
        )
