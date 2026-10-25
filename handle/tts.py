"""/tts — text to speech with language + voice selection by buttons.

Flow: reply to a text with /tts  ->  pick language  ->  pick voice  ->
the job goes into the per-user queue (task_queue.py).

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
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import MAX_TEXT_LENGTH, COOLDOWN, TTS_TIMEOUT
from task_queue import enqueue, task_queue

log = logging.getLogger(__name__)

SELECTION_TIMEOUT = 90  # seconds the language/voice menu stays alive

# Every language has a female + male voice. English also keeps the extra
# "calm" and "energetic" voices of the old version.
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
# user_id -> key of that user's open menu (one menu per user)
user_menu = {}
_background = set()


# ───────────────────────── helpers ─────────────────────────
def resolve_voice(lang_key, style_key):
    """Return (voice_name, rate, pitch) for a language + style."""
    voices = LANGUAGES[lang_key]["voices"]
    _, slot, rate, pitch = STYLES[style_key]
    voice = voices.get(slot) or voices[SLOT_FALLBACK.get(slot, slot)]
    return voice, rate, pitch


def language_keyboard():
    keys = list(LANGUAGES)
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    LANGUAGES[k]["label"], callback_data=f"tts:lang:{k}"
                )
                for k in keys[i:i + 2]
            ]
        )
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="tts:cancel")])
    return InlineKeyboardMarkup(rows)


def voice_keyboard():
    keys = list(STYLES)
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    STYLES[k][0], callback_data=f"tts:voice:{k}"
                )
                for k in keys[i:i + 2]
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="tts:back"),
            InlineKeyboardButton("✖️ Cancel", callback_data="tts:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


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
    """Close the menu if the user never finishes choosing."""
    while True:
        session = sessions.get(key)
        if not session:
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
    """Generate an mp3 and return its path. Cancels cleanly on timeout."""
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

    finally:
        if not succeeded and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


def build_job(message, text, lang_key, style_key):
    language = LANGUAGES[lang_key]["label"]
    voice, rate, pitch = resolve_voice(lang_key, style_key)
    style_label = STYLES[style_key][0]

    async def job():
        status = None
        audio_path = None
        try:
            status = await safe_reply(message, "🔊 Generating audio...")

            try:
                audio_path = await create_audio_file(text, voice, rate, pitch)
            except NoAudioReceived:
                await safe_reply(
                    message,
                    "⚠️ No speech could be made from this text. "
                    "Make sure the language you picked matches your text.",
                )
                return

            if status is not None:
                await safe_edit(status, "📤 Audio generated. Uploading...")

            await message.reply_audio(
                audio=audio_path,
                caption=(
                    "🔊 Your speech is ready!\n"
                    f"🌍 Language: {language}\n"
                    f"🎙️ Voice: {style_label}"
                ),
                title="Text to Speech",
            )

        finally:
            await safe_delete(status)
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except OSError:
                    pass

    return job


# ───────────────────────── registration ─────────────────────────
def register_tts(app):
    """Register /tts and its language / voice buttons."""

    @app.on_message(filters.command("tts"))
    async def tts_command(client, message):
        user = message.from_user
        if not user:
            return

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

        # One open menu per user: a new /tts replaces the old one.
        old_key = user_menu.get(user.id)
        if old_key:
            old = drop_session(old_key)
            if old:
                await safe_edit(old["menu"], "✖️ Replaced by a newer /tts request.")

        try:
            menu = await message.reply_text(
                "🌍 Choose the language of your text:",
                reply_markup=language_keyboard(),
            )
        except Exception:
            await safe_reply(message, "❌ Couldn't show the language menu.")
            return

        key = (menu.chat.id, menu.id)
        sessions[key] = {
            "user_id": user.id,
            "text": text,
            "message": message,
            "menu": menu,
            "lang": None,
            "expires": time.monotonic() + SELECTION_TIMEOUT,
        }
        user_menu[user.id] = key

        task = asyncio.create_task(expire_session(key))
        _background.add(task)
        task.add_done_callback(_background.discard)

    @app.on_callback_query(
        filters.regex(r"^tts:(lang:[a-z]{2}|voice:[a-z_]+|back|cancel)$")
    )
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
            return

        if session["user_id"] != user.id:
            await query.answer(
                "This menu belongs to another user.", show_alert=True
            )
            return

        session["expires"] = time.monotonic() + SELECTION_TIMEOUT
        parts = query.data.split(":")
        action = parts[1]

        # ── cancel ──
        if action == "cancel":
            drop_session(key)
            await query.answer("TTS request cancelled.")
            await safe_edit(query.message, "✖️ TTS request cancelled.")
            return

        # ── back to language list ──
        if action == "back":
            session["lang"] = None
            await query.answer()
            await safe_edit(
                query.message,
                "🌍 Choose the language of your text:",
                language_keyboard(),
            )
            return

        # ── language chosen -> show voices ──
        if action == "lang":
            lang_key = parts[2]
            if lang_key not in LANGUAGES:
                await query.answer("Unknown language.", show_alert=True)
                return
            session["lang"] = lang_key
            await query.answer()
            await safe_edit(
                query.message,
                f"🎙️ Choose a voice for {LANGUAGES[lang_key]['label']}:",
                voice_keyboard(),
            )
            return

        # ── voice chosen -> queue the job ──
        style_key = parts[2]
        lang_key = session["lang"]
        if style_key not in STYLES or lang_key not in LANGUAGES:
            await query.answer(
                "Please choose a language first.", show_alert=True
            )
            return

        drop_session(key)  # prevents a double-tap from queueing twice
        await query.answer("Voice selected!")

        language = LANGUAGES[lang_key]["label"]
        style_label = STYLES[style_key][0]
        details = f"🌍 Language: {language}\n🎙️ Voice: {style_label}"

        await safe_edit(query.message, f"⏳ Processing your request...\n{details}")

        job = build_job(session["message"], session["text"], lang_key, style_key)
        accepted = await enqueue(
            session["message"],
            user.id,
            job,
            kind="tts",
            command="/tts",
            timeout=float(TTS_TIMEOUT) + 60,
            cooldown=COOLDOWN,
            error_text="❌ Couldn't generate or send the speech. Please try again later.",
            timeout_text=(
                "⏳ Speech generation timed out. The task was stopped; "
                "you can try again shortly."
            ),
        )

        if accepted:
            snap = task_queue.snapshot()
            await safe_edit(
                query.message,
                f"✅ Request accepted.\n{details}\n"
                f"📋 Tasks in system: {snap['in_system']}/{snap['capacity']}",
            )
        else:
            await safe_edit(
                query.message,
                "✖️ Your request wasn't queued. Reply to your text with /tts "
                "to try again.",
            )
