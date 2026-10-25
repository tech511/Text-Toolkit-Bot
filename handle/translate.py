"""/translate — translate text from one language to another with buttons.

Flow: reply to a text with /translate -> pick the language of the text ->
pick the language to translate into -> result.

Supported: English, Japanese, Korean, Russian, Bangla, Hindi, Chinese.
Uses Google's free web endpoint through aiohttp (no API key, no extra
dependency — aiohttp is already installed by edge-tts).
"""

import asyncio
import logging
import time

import aiohttp
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import MAX_TEXT_LENGTH, TRANSLATE_COOLDOWN, TRANSLATE_TIMEOUT
from task_queue import enqueue, task_queue

log = logging.getLogger(__name__)

SELECTION_TIMEOUT = 90
API_URL = "https://translate.googleapis.com/translate_a/single"

# key -> (button label, API language code)
LANGUAGES = {
    "en": ("🇬🇧 English", "en"),
    "ja": ("🇯🇵 Japanese", "ja"),
    "ko": ("🇰🇷 Korean", "ko"),
    "ru": ("🇷🇺 Russian", "ru"),
    "bn": ("🇧🇩 Bangla", "bn"),
    "hi": ("🇮🇳 Hindi", "hi"),
    "zh": ("🇨🇳 Chinese", "zh-CN"),
}

# (chat_id, menu_message_id) -> session
sessions = {}
user_menu = {}
_background = set()


# ───────────────────────── helpers ─────────────────────────
def _grid(keys, prefix):
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    LANGUAGES[k][0], callback_data=f"tr:{prefix}:{k}"
                )
                for k in keys[i:i + 2]
            ]
        )
    return rows


def from_keyboard():
    rows = _grid(list(LANGUAGES), "from")
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data="tr:cancel")])
    return InlineKeyboardMarkup(rows)


def to_keyboard(source_key):
    keys = [k for k in LANGUAGES if k != source_key]
    rows = _grid(keys, "to")
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data="tr:back"),
            InlineKeyboardButton("✖️ Cancel", callback_data="tr:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def safe_reply(message, text, **kwargs):
    try:
        return await message.reply_text(text, **kwargs)
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
            "⌛ Selection expired. Reply to a text with /translate to try again.",
        )


# ───────────────────────── translation ─────────────────────────
async def translate_text(text, source_code, target_code):
    params = {
        "client": "gtx",
        "sl": source_code,
        "tl": target_code,
        "dt": "t",
        "q": text,
    }
    timeout = aiohttp.ClientTimeout(total=max(5, float(TRANSLATE_TIMEOUT)))

    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
    ) as http:
        async with http.get(API_URL, params=params) as response:
            if response.status != 200:
                raise RuntimeError(f"Translate API returned HTTP {response.status}")
            data = await response.json(content_type=None)

    try:
        parts = [segment[0] for segment in data[0] if segment and segment[0]]
    except (TypeError, IndexError, KeyError) as exc:
        raise RuntimeError("Unexpected translate API response") from exc

    result = "".join(parts).strip()
    if not result:
        raise RuntimeError("Empty translation")
    return result


def build_job(message, text, source_key, target_key):
    source_label, source_code = LANGUAGES[source_key]
    target_label, target_code = LANGUAGES[target_key]

    async def job():
        status = await safe_reply(message, "🌐 Translating...")
        try:
            result = await translate_text(text, source_code, target_code)
            output = f"🌐 {source_label} ➜ {target_label}\n\n{result}"
            await message.reply_text(
                output[:4096],
                parse_mode=ParseMode.DISABLED,
                disable_web_page_preview=True,
            )
        finally:
            await safe_delete(status)

    return job


# ───────────────────────── registration ─────────────────────────
def register_translate(app):
    """Register /translate and its language buttons."""

    @app.on_message(filters.command("translate"))
    async def translate_command(client, message):
        user = message.from_user
        if not user:
            return

        source = message.reply_to_message
        text = ((source.text or source.caption or "") if source else "").strip()

        if not text:
            await safe_reply(
                message,
                "📝 Send a text message, then reply to it with /translate.",
            )
            return

        if len(text) > MAX_TEXT_LENGTH:
            await safe_reply(
                message,
                f"⚠️ Please keep your text under {MAX_TEXT_LENGTH} characters.",
            )
            return

        wait = task_queue.cooldown_left(user.id, "translate")
        if wait > 0:
            await safe_reply(
                message,
                f"⏳ Please wait {int(wait) + 1} second(s) before using /translate again.",
            )
            return

        old_key = user_menu.get(user.id)
        if old_key:
            old = drop_session(old_key)
            if old:
                await safe_edit(
                    old["menu"], "✖️ Replaced by a newer /translate request."
                )

        menu = await safe_reply(
            message,
            "🌍 Which language is your text written in?",
            reply_markup=from_keyboard(),
        )
        if menu is None:
            return

        key = (menu.chat.id, menu.id)
        sessions[key] = {
            "user_id": user.id,
            "text": text,
            "message": message,
            "menu": menu,
            "source": None,
            "expires": time.monotonic() + SELECTION_TIMEOUT,
        }
        user_menu[user.id] = key

        task = asyncio.create_task(expire_session(key))
        _background.add(task)
        task.add_done_callback(_background.discard)

    @app.on_callback_query(
        filters.regex(r"^tr:(from:[a-z]{2}|to:[a-z]{2}|back|cancel)$")
    )
    async def translate_callback(client, query):
        user = query.from_user
        if not user or not query.message:
            await query.answer("Unable to identify you.", show_alert=True)
            return

        key = (query.message.chat.id, query.message.id)
        session = sessions.get(key)

        if not session:
            await query.answer(
                "This menu has expired. Send /translate again.", show_alert=True
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

        if action == "cancel":
            drop_session(key)
            await query.answer("Translation cancelled.")
            await safe_edit(query.message, "✖️ Translation cancelled.")
            return

        if action == "back":
            session["source"] = None
            await query.answer()
            await safe_edit(
                query.message,
                "🌍 Which language is your text written in?",
                from_keyboard(),
            )
            return

        if action == "from":
            source_key = parts[2]
            if source_key not in LANGUAGES:
                await query.answer("Unknown language.", show_alert=True)
                return
            session["source"] = source_key
            await query.answer()
            await safe_edit(
                query.message,
                f"🎯 Translate from {LANGUAGES[source_key][0]} into:",
                to_keyboard(source_key),
            )
            return

        # action == "to"
        target_key = parts[2]
        source_key = session["source"]
        if (
            target_key not in LANGUAGES
            or source_key not in LANGUAGES
            or target_key == source_key
        ):
            await query.answer(
                "Please choose the source language first.", show_alert=True
            )
            return

        drop_session(key)  # a double-tap can't queue the job twice
        await query.answer("Translating...")

        details = f"{LANGUAGES[source_key][0]} ➜ {LANGUAGES[target_key][0]}"
        await safe_edit(query.message, f"⏳ Processing your request...\n🌐 {details}")

        job = build_job(session["message"], session["text"], source_key, target_key)
        accepted = await enqueue(
            session["message"],
            user.id,
            job,
            kind="translate",
            command="/translate",
            timeout=float(TRANSLATE_TIMEOUT) + 20,
            cooldown=TRANSLATE_COOLDOWN,
            error_text="❌ Translation failed. Please try again in a moment.",
            timeout_text="⏳ Translation took too long and was stopped. Please try again.",
        )

        await safe_edit(
            query.message,
            f"✅ Request accepted.\n🌐 {details}"
            if accepted
            else "✖️ Your request wasn't queued. Reply to your text with /translate to try again.",
        )
