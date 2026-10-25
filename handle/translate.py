"""/translate — translate text into another language with buttons.

Flow
----
1. Reply to a text with /translate.
2. The bot detects the language of the text by itself.
   * Not supported / no readable text -> the bot answers with the list of
     supported languages (no menu, no cooldown).
   * Supported -> the bot only asks which language to translate INTO. The
     detected language is never offered (English text -> no English button,
     Japanese text -> no Japanese button, ...).
3. The job goes into the per-user queue (task_queue.py).

Rules
-----
* Close button: removes the menu and stops the task (deleted from the queue
  if it is still waiting, cancelled if it is already running). Closing NEVER
  starts a cooldown.
* Cooldown (TRANSLATE_COOLDOWN seconds) starts ONLY after a translation was
  really delivered. Failed / timed out / cancelled requests cost nothing.
* The owner (OWNER_ID) has no cooldown, no task limit and no queue limit.

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
from pyrogram.errors import BadRequest
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import MAX_TEXT_LENGTH, TRANSLATE_COOLDOWN, TRANSLATE_TIMEOUT
from lang_detect import NO_TEXT, detect_language
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
_latest_request = {}
_background = set()


class _Retryable(Exception):
    """Temporary API problem (HTTP 429 / 5xx) - worth one more try."""


# ───────────────────────── helpers ─────────────────────────
def close_button():
    return InlineKeyboardButton("✖️ Close", callback_data="tr:close")


def close_keyboard():
    return InlineKeyboardMarkup([[close_button()]])


def to_keyboard(source_key):
    """Target languages: every supported language EXCEPT the detected one."""
    keys = [k for k in LANGUAGES if k != source_key]
    rows = []
    for i in range(0, len(keys), 2):
        rows.append(
            [
                InlineKeyboardButton(LANGUAGES[k][0], callback_data=f"tr:to:{k}")
                for k in keys[i:i + 2]
            ]
        )
    rows.append([close_button()])
    return InlineKeyboardMarkup(rows)


def supported_languages_text(reason=None):
    if reason == NO_TEXT:
        head = "⚠️ I couldn't find any readable text to translate."
    else:
        head = (
            "⚠️ I couldn't recognise the language of this text, "
            "or it isn't supported yet."
        )
    languages = "\n".join(f"• {label}" for label, _ in LANGUAGES.values())
    return (
        f"{head}\n\n🌍 Supported languages:\n{languages}\n\n"
        "Reply to a text written in one of these languages with /translate."
    )


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
    """Close the menu if the user never picks a target language."""
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
            "⌛ Selection expired. Reply to a text with /translate to try again.",
        )


# ───────────────────────── translation ─────────────────────────
async def _translate_once(text, source_code, target_code):
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
            if response.status == 429 or response.status >= 500:
                raise _Retryable(f"Translate API returned HTTP {response.status}")
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


async def translate_text(text, source_code, target_code):
    """Translate with one quick retry for temporary network / server errors."""
    for attempt in (1, 2):
        try:
            return await _translate_once(text, source_code, target_code)
        except (_Retryable, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("translate attempt %s failed: %r", attempt, exc)
            if attempt == 2:
                raise
            await asyncio.sleep(1)


async def send_result(client, message, output):
    """Send the translation as plain text.

    Tries the full set of options first, then simpler ones, so it works on
    every Pyrogram version / fork. If the command message is gone it sends a
    normal message instead of a reply.
    """
    disabled = getattr(ParseMode, "DISABLED", None)
    variants = []
    if disabled is not None:
        variants.append(dict(parse_mode=disabled, disable_web_page_preview=True))
        variants.append(dict(parse_mode=disabled))
    variants.append({})

    last_error = None
    for kwargs in variants:
        try:
            await message.reply_text(output, **kwargs)
            return
        except (TypeError, ValueError) as exc:  # option not supported here
            last_error = exc
            log.warning("reply_text rejected %s: %r", list(kwargs), exc)
        except BadRequest:
            # e.g. the user deleted the /translate message
            try:
                await client.send_message(
                    chat_id=message.chat.id, text=output, **kwargs
                )
                return
            except (TypeError, ValueError) as exc:
                last_error = exc
    raise last_error or RuntimeError("Could not send the translation")


def build_job(client, key, session, source_key, target_key, details):
    message = session["message"]
    menu = session["menu"]
    text = session["text"]
    source_label, source_code = LANGUAGES[source_key]
    target_label, target_code = LANGUAGES[target_key]

    async def job():
        try:
            await safe_edit(menu, f"🌐 Translating...\n{details}", close_keyboard())

            try:
                result = await translate_text(text, source_code, target_code)
            except Exception:
                log.exception(
                    "translate request failed (%s -> %s)", source_code, target_code
                )
                raise

            output = f"🌐 {source_label} ➜ {target_label}\n\n{result}"

            try:
                await send_result(client, message, output[:4096])
            except Exception:
                log.exception("could not send the translation")
                raise

            return True  # delivered -> cooldown starts now
        finally:
            drop_session(key)
            await safe_delete(menu)

    return job


# ───────────────────────── registration ─────────────────────────
def register_translate(app):
    """Register /translate and its target-language buttons."""

    @app.on_message(filters.command("translate"))
    async def translate_command(client, message):
        user = message.from_user
        if not user:
            return

        is_owner = task_queue.is_owner(user.id)

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

        # The owner has no cooldown and no task limit.
        if not is_owner:
            wait = task_queue.cooldown_left(user.id, "translate")
            if wait > 0:
                await safe_reply(
                    message,
                    f"⏳ Please wait {int(wait) + 1} second(s) before using "
                    "/translate again.",
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
            return  # the user sent a newer /translate while we were detecting
        if _latest_request.get(user.id) is token:
            _latest_request.pop(user.id, None)

        if not detection.supported:
            await safe_reply(message, supported_languages_text(detection.reason))
            return

        source_key = detection.key
        if source_key not in LANGUAGES:  # defensive
            await safe_reply(message, supported_languages_text())
            return

        # One open menu per normal user: a new /translate replaces the old one.
        if not is_owner:
            old_key = user_menu.get(user.id)
            if old_key:
                old = drop_session(old_key)
                if old:
                    await safe_edit(
                        old["menu"], "✖️ Replaced by a newer /translate request."
                    )

        menu = await safe_reply(
            message,
            f"🌍 Detected language: {LANGUAGES[source_key][0]}\n"
            "🎯 Translate into:",
            reply_markup=to_keyboard(source_key),
        )
        if menu is None:
            await safe_reply(message, "❌ Couldn't show the language menu.")
            return

        key = (menu.chat.id, menu.id)
        sessions[key] = {
            "user_id": user.id,
            "text": text,
            "message": message,
            "menu": menu,
            "source": source_key,
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

    @app.on_callback_query(filters.regex(r"^tr:"))
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
        if action in ("close", "cancel"):
            session["closed"] = True
            drop_session(key)
            await query.answer("Closed.")
            await task_queue.cancel(session.get("task"))
            await safe_delete(query.message)
            return

        # ── target chosen -> queue the job ──
        if action != "to" or len(parts) < 3:
            await query.answer("This button is no longer valid.", show_alert=True)
            return

        if session["state"] != "choosing":
            await query.answer("Your request is already being processed.")
            return

        target_key = parts[2]
        source_key = session["source"]
        if (
            target_key not in LANGUAGES
            or source_key not in LANGUAGES
            or target_key == source_key
        ):
            await query.answer("Unknown language.", show_alert=True)
            return

        # Lock the session before the first await: a double-tap can't queue
        # the same request twice.
        session["state"] = "queued"
        session["expires"] = None
        await query.answer("Translating...")

        details = f"{LANGUAGES[source_key][0]} ➜ {LANGUAGES[target_key][0]}"
        await safe_edit(
            query.message,
            f"⏳ Processing your request...\n🌐 {details}",
            close_keyboard(),
        )

        job = build_job(client, key, session, source_key, target_key, details)
        result = await enqueue(
            session["message"],
            user.id,
            job,
            kind="translate",
            command="/translate",
            timeout=float(TRANSLATE_TIMEOUT) * 2 + 20,
            cooldown=TRANSLATE_COOLDOWN,
            cooldown_on_success=True,   # cooldown only after a real success
            show_position=False,        # the menu message shows the position
            error_text="❌ Translation failed. Please try again in a moment.",
            timeout_text=(
                "⏳ Translation took too long and was stopped. Please try again."
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
                f"📋 In queue — position #{result.position}\n🌐 {details}\n"
                "I'll start as soon as it's your turn.",
                close_keyboard(),
            )
