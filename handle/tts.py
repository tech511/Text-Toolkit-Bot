"""/tts — text to speech (mp3) with automatic language detection.

Flow
----
1. Reply to a text with /tts.
2. The bot detects the language of the text by itself.
   * Not supported / no readable text -> the bot answers with the list of
     supported languages (no menu, no cooldown).
   * Supported -> the bot only asks for the *voice* (buttons) + a Close button.
3. The job goes into the per-user queue (task_queue.py).

Rules
-----
* Close button: removes the menu and stops the task (it is deleted from the
  queue if it is still waiting, or cancelled if it is already running).
  Closing NEVER starts a cooldown.
* Cooldown (COOLDOWN seconds) starts ONLY after an audio file was really
  generated and sent. Failed / timed out / cancelled / "no speech" requests
  never cost the user a cooldown.
* The owner (OWNER_ID) has no cooldown, no task limit and no queue limit.
* Output is always an mp3 audio file.

Supported languages: English, Japanese, Korean, Russian, Bangla, Hindi,
Chinese (Microsoft Edge neural voices through the ``edge-tts`` package).
"""

import asyncio
import logging
import os
import tempfile
import time

import edge_tts
from edge_tts.exceptions import NoAudioReceived
from pyrogram import filters
from pyrogram.errors import BadRequest
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import MAX_TEXT_LENGTH, COOLDOWN, TTS_TIMEOUT
from lang_detect import NO_TEXT, detect_language
from task_queue import enqueue, task_queue

log = logging.getLogger(__name__)

SELECTION_TIMEOUT = 90  # seconds the voice menu stays alive

# Every language has a female + male voice. English also keeps the extra
# "calm" and "energetic" voices.
LANGUAGES = {
    "en": {
        "label": "🇬🇧 English",
        "voices": {
            "female": "en-US-AriaNeural",
            "male": "en-US-GuyNeural",
            "calm": "en-US-JennyNeural",
            "energetic": "en-US-DavisNeural",
        },
    },
    "ja": {
        "label": "🇯🇵 Japanese",
        "voices": {"female": "ja-JP-NanamiNeural", "male": "ja-JP-KeitaNeural"},
    },
    "ko": {
        "label": "🇰🇷 Korean",
        "voices": {"female": "ko-KR-SunHiNeural", "male": "ko-KR-InJoonNeural"},
    },
    "ru": {
        "label": "🇷🇺 Russian",
        "voices": {"female": "ru-RU-SvetlanaNeural", "male": "ru-RU-DmitryNeural"},
    },
    "bn": {
        "label": "🇧🇩 Bangla",
        "voices": {"female": "bn-BD-NabanitaNeural", "male": "bn-BD-PradeepNeural"},
    },
    "hi": {
        "label": "🇮🇳 Hindi",
        "voices": {"female": "hi-IN-SwaraNeural", "male": "hi-IN-MadhurNeural"},
    },
    "zh": {
        "label": "🇨🇳 Chinese",
        "voices": {"female": "zh-CN-XiaoxiaoNeural", "male": "zh-CN-YunxiNeural"},
    },
}

# style -> (button label, voice slot, rate, pitch)
STYLES = {
    "female": ("👩 Female", "female", "+0%", "+0Hz"),
    "male": ("👨 Male", "male", "+0%", "+0Hz"),
    "deep_male": ("🎙️ Deep Male", "male", "-5%", "-12Hz"),
    "calm": ("🌙 Soft / Calm", "calm", "-12%", "-2Hz"),
    "energetic": ("⚡ Fast / Energetic", "energetic", "+12%", "+0Hz"),
    "bright": ("😊 Bright Female", "female", "+8%", "+6Hz"),
}

# Slots a language may not have fall back to its female / male voice.
SLOT_FALLBACK = {"calm": "female", "energetic": "male"}

# (chat_id, menu_message_id) -> session
sessions = {}
# user_id -> key of that user's open menu (one menu per normal user)
user_menu = {}
# user_id -> token of the newest /tts command (stops double-command races)
_latest_request = {}
_background = set()


# ───────────────────────── helpers ─────────────────────────
def resolve_voice(lang_key, style_key):
    """Return (voice_name, rate, pitch) for a language + style."""
    voices = LANGUAGES[lang_key]["voices"]
    _, slot, rate, pitch = STYLES[style_key]
    voice = voices.get(slot) or voices[SLOT_FALLBACK.get(slot, slot)]
    return voice, rate, pitch


def close_button():
    return InlineKeyboardButton("✖️ Close", callback_data="tts:close")


def close_keyboard():
    return InlineKeyboardMarkup([[close_button()]])


def voice_keyboard():
    keys = list(STYLES)
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(STYLES[k][0], callback_data=f"tts:voice:{k}")
                for k in keys[i:i + 2]
            ]
        )
    rows.append([close_button()])
    return InlineKeyboardMarkup(rows)


def supported_languages_text(reason=None):
    if reason == NO_TEXT:
        head = "⚠️ I couldn't find any readable text to speak."
    else:
        head = (
            "⚠️ I couldn't recognise the language of this text, "
            "or it isn't supported yet."
        )
    languages = "\n".join(f"• {info['label']}" for info in LANGUAGES.values())
    return (
        f"{head}\n\n🌍 Supported languages:\n{languages}\n\n"
        "Reply to a text written in one of these languages with /tts."
    )


async def safe_reply(message, text):
    try:
        return await message.reply_text(text)
    except Exception:
        return None


async def safe_edit(message, text, markup=None):
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


async def safe_delete(message):
    if message is not None:
        try:
            await message.delete()
        except Exception:
            pass


def drop_session(key):
    session = sessions.pop(key, None)
    if session and user_menu.get(session["user_id"]) == key:
        user_menu.pop(session["user_id"], None)
    return session


async def expire_session(key):
    """Close the menu if the user never picks a voice."""
    while True:
        session = sessions.get(key)
        if not session or session["state"] != "choosing":
            return
        left = session["expires"] - time.monotonic()
        if left <= 0:
            break
        await asyncio.sleep(left + 0.2)

    session = drop_session(key)
    if session:
        await safe_edit(
            session["menu"],
            "⌛ Selection expired. Reply to a text with /tts to try again.",
        )


# ───────────────────────── audio ─────────────────────────
async def create_audio_file(text, voice, rate, pitch):
    """Generate an mp3 and return its path.

    One quick retry for transient network errors. Never leaves a temp file
    behind when something goes wrong.
    """
    last_error = None
    for attempt in (1, 2):
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        succeeded = False
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            await asyncio.wait_for(
                communicate.save(path),
                timeout=max(1, float(TTS_TIMEOUT)),
            )
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise RuntimeError("Edge TTS returned an empty audio file.")
            succeeded = True
            return path

        except (NoAudioReceived, asyncio.TimeoutError):
            raise  # retrying will not help
        except Exception as exc:
            last_error = exc
            log.warning("edge-tts attempt %s failed: %r", attempt, exc)
            if attempt == 2:
                raise
            await asyncio.sleep(1)
        finally:
            if not succeeded and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    raise last_error  # pragma: no cover (loop always returns or raises)


async def send_audio(client, message, audio_path, caption):
    """Reply with the mp3; if the command message is gone, just send it."""
    kwargs = dict(
        audio=audio_path,
        caption=caption,
        title="Text to Speech",
        file_name="speech.mp3",
    )
    try:
        await message.reply_audio(**kwargs)
    except BadRequest:
        # e.g. the user deleted the /tts message -> can't reply to it.
        await client.send_audio(chat_id=message.chat.id, **kwargs)


def build_job(client, key, session, lang_key, style_key, details):
    message = session["message"]
    menu = session["menu"]
    text = session["text"]
    language = LANGUAGES[lang_key]["label"]
    voice, rate, pitch = resolve_voice(lang_key, style_key)
    style_label = STYLES[style_key][0]

    async def job():
        audio_path = None
        try:
            await safe_edit(menu, f"🔊 Generating audio...\n{details}", close_keyboard())

            try:
                audio_path = await create_audio_file(text, voice, rate, pitch)
            except NoAudioReceived:
                await safe_reply(
                    message,
                    "⚠️ No speech could be made from this text. "
                    "Please try a different text.",
                )
                return False  # finished but nothing delivered -> no cooldown

            await safe_edit(menu, f"📤 Uploading...\n{details}", close_keyboard())

            await send_audio(
                client,
                message,
                audio_path,
                (
                    "🔊 Your speech is ready!\n"
                    f"🌍 Language: {language}\n"
                    f"🎙️ Voice: {style_label}"
                ),
            )
            return True  # delivered -> cooldown starts now

        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass
            drop_session(key)
            await safe_delete(menu)

    return job


# ───────────────────────── registration ─────────────────────────
def register_tts(app):
    """Register /tts and its voice / close buttons."""

    @app.on_message(filters.command("tts"))
    async def tts_command(client, message):
        user = message.from_user
        if not user:
            return

        is_owner = task_queue.is_owner(user.id)

        source = message.reply_to_message
        text = ((source.text or source.caption or "") if source else "").strip()

        if not text:
            await safe_reply(
                message,
                "📝 Send a text message, then reply to it with /tts.",
            )
            return

        if len(text) > MAX_TEXT_LENGTH:
            await safe_reply(
                message,
                f"⚠️ Please keep your text under {MAX_TEXT_LENGTH} characters.",
            )
            return

        # The owner has no cooldown and no task limit.
        if not is_owner:
            wait = task_queue.cooldown_left(user.id, "tts")
            if wait > 0:
                await safe_reply(
                    message,
                    f"⏳ Please wait {int(wait) + 1} seconds before another request.",
                )
                return

            if task_queue.user_load(user.id) > 0:
                await safe_reply(
                    message,
                    "⏳ You already have a task running or waiting.",
                )
                return

        token = object()
        _latest_request[user.id] = token

        # ── detect the language automatically ──
        detection = await detect_language(text)

        if not is_owner and _latest_request.get(user.id) is not token:
            return  # the user sent a newer /tts while we were detecting
        if _latest_request.get(user.id) is token:
            _latest_request.pop(user.id, None)

        if not detection.supported:
            await safe_reply(message, supported_languages_text(detection.reason))
            return

        lang_key = detection.key
        if lang_key not in LANGUAGES:  # defensive
            await safe_reply(message, supported_languages_text())
            return

        # One open menu per normal user: a new /tts replaces the old one.
        if not is_owner:
            old_key = user_menu.get(user.id)
            if old_key:
                old = drop_session(old_key)
                if old:
                    await safe_edit(old["menu"], "✖️ Replaced by a newer /tts request.")

        try:
            menu = await message.reply_text(
                f"🌍 Detected language: {LANGUAGES[lang_key]['label']}\n"
                "🎙️ Choose a voice:",
                reply_markup=voice_keyboard(),
            )
        except Exception:
            await safe_reply(message, "❌ Couldn't show the voice menu.")
            return

        key = (menu.chat.id, menu.id)
        sessions[key] = {
            "user_id": user.id,
            "text": text,
            "message": message,
            "menu": menu,
            "lang": lang_key,
            "state": "choosing",     # choosing -> queued
            "closed": False,
            "task": None,
            "expires": time.monotonic() + SELECTION_TIMEOUT,
        }
        if not is_owner:
            user_menu[user.id] = key

        task = asyncio.create_task(expire_session(key))
        _background.add(task)
        task.add_done_callback(_background.discard)

    @app.on_callback_query(filters.regex(r"^tts:"))
    async def tts_callback(client, query):
        user = query.from_user
        if not user or not query.message:
            await query.answer("Unable to identify you.", show_alert=True)
            return

        key = (query.message.chat.id, query.message.id)
        session = sessions.get(key)

        if not session:
            await query.answer(
                "This menu has expired. Send /tts again.", show_alert=True
            )
            # Remove the dead buttons so they can't be tapped again.
            await safe_edit(query.message, "⌛ This menu has expired.")
            return

        if session["user_id"] != user.id:
            await query.answer(
                "This menu belongs to another user.", show_alert=True
            )
            return

        parts = (query.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""

        # ── close: delete the menu, stop the task, NO cooldown ──
        if action == "close":
            session["closed"] = True
            drop_session(key)
            await query.answer("Closed.")
            await task_queue.cancel(session.get("task"))
            await safe_delete(query.message)
            return

        # ── voice chosen -> queue the job ──
        if action != "voice" or len(parts) < 3:
            await query.answer("This button is no longer valid.", show_alert=True)
            return

        if session["state"] != "choosing":
            await query.answer("Your request is already being processed.")
            return

        style_key = parts[2]
        lang_key = session["lang"]
        if style_key not in STYLES or lang_key not in LANGUAGES:
            await query.answer("Unknown voice.", show_alert=True)
            return

        # Lock the session before the first await: a double-tap can't queue
        # the same request twice.
        session["state"] = "queued"
        session["expires"] = None
        await query.answer("Voice selected!")

        details = (
            f"🌍 Language: {LANGUAGES[lang_key]['label']}\n"
            f"🎙️ Voice: {STYLES[style_key][0]}"
        )
        await safe_edit(
            query.message, f"⏳ Processing your request...\n{details}", close_keyboard()
        )

        job = build_job(client, key, session, lang_key, style_key, details)
        result = await enqueue(
            session["message"],
            user.id,
            job,
            kind="tts",
            command="/tts",
            timeout=float(TTS_TIMEOUT) * 2 + 30,
            cooldown=COOLDOWN,
            cooldown_on_success=True,   # cooldown only after a real success
            show_position=False,        # the menu message shows the position
            error_text=(
                "❌ Couldn't generate or send the speech. Please try again later."
            ),
            timeout_text=(
                "⏳ Speech generation timed out. The task was stopped; "
                "you can try again shortly."
            ),
        )

        if not result:
            # enqueue() already explained why (limit / full queue).
            drop_session(key)
            await safe_delete(query.message)
            return

        session["task"] = result.task

        if session["closed"]:
            # Close was pressed while the request was being queued.
            await task_queue.cancel(result.task)
            drop_session(key)
            await safe_delete(query.message)
            return

        if result.position > 0 and result.task.state == "waiting":
            await safe_edit(
                query.message,
                f"📋 In queue — position #{result.position}\n{details}\n"
                "I'll start as soon as it's your turn.",
                close_keyboard(),
            )
