"""MongoDB user storage (light: one collection, one small document per user).

Document layout (collection ``users``)::

    _id         int   Telegram user id
    first_name  str
    last_name   str | None
    username    str | None
    joined_at   datetime (UTC)
    has_info    bool  True once the profile fields above were saved
    started     bool  True once the user sent /start
    banned      bool
    banned_at / banned_by   (only when banned)

User info is saved ONE time only. If the user is already stored, nothing is
written again (a small in-memory set also avoids even asking the database).
"""

import html
import logging
import re
import time
from datetime import datetime, timezone

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pymongo import AsyncMongoClient
from pymongo.errors import DuplicateKeyError

from config import DB_NAME, LOG_CHANNEL, MONGO_URI

log = logging.getLogger(__name__)

# ───────────────────────── connection (lazy) ─────────────────────────
_client = None
_users = None
_indexed = False


async def _col():
    """Return the users collection, creating the client inside the bot's loop."""
    global _client, _users, _indexed

    if _users is None:
        if not MONGO_URI:
            raise RuntimeError("MONGO_URI is not set")
        _client = AsyncMongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            maxPoolSize=10,
            appname="text-toolkit-bot",
        )
        _users = _client[DB_NAME]["users"]

    if not _indexed:
        try:
            await _users.create_index("started")
            await _users.create_index("banned")
            _indexed = True
        except Exception:
            log.warning("could not create indexes (will retry)", exc_info=True)

    return _users


def _now():
    return datetime.now(timezone.utc)


# ───────────────────────── live users (memory only) ─────────────────────────
_last_seen: dict = {}


def touch(user_id: int) -> None:
    """Remember that this user was active just now."""
    now = time.monotonic()
    _last_seen[user_id] = now
    if len(_last_seen) > 20000:
        for uid, seen in list(_last_seen.items()):
            if now - seen > 3600:
                _last_seen.pop(uid, None)


def live_users(window: int = 300) -> int:
    now = time.monotonic()
    return sum(1 for seen in _last_seen.values() if now - seen <= window)


# ───────────────────────── saving users ─────────────────────────
_seen: set = set()          # users already saved (skips useless DB calls)
_started: set = set()       # users whose /start flag is already saved
_MAX_CACHE = 50000


def _cap(cache: set) -> None:
    if len(cache) > _MAX_CACHE:
        cache.clear()


async def track_user(user) -> bool:
    """Save first name, last name, id and username ONE time.

    Returns True only the first time this user is saved.
    """
    uid = user.id
    if uid in _seen:
        return False

    col = await _col()
    try:
        result = await col.update_one(
            {"_id": uid, "has_info": {"$ne": True}},
            {
                "$set": {
                    "first_name": user.first_name or "",
                    "last_name": user.last_name,
                    "username": user.username,
                    "joined_at": _now(),
                    "has_info": True,
                },
                "$setOnInsert": {"banned": False, "started": False},
            },
            upsert=True,
        )
        is_new = result.upserted_id is not None or result.matched_count == 1
    except DuplicateKeyError:
        # The profile was already saved earlier -> not a new user.
        is_new = False

    _cap(_seen)
    _seen.add(uid)
    return is_new


async def mark_started(user_id: int) -> None:
    """Remember that the user sent /start (used for the 'total users' stat)."""
    if user_id in _started:
        return
    col = await _col()
    await col.update_one(
        {"_id": user_id},
        {"$set": {"started": True}, "$setOnInsert": {"banned": False}},
        upsert=True,
    )
    _cap(_started)
    _started.add(user_id)


async def get_user(user_id: int):
    col = await _col()
    return await col.find_one({"_id": user_id})


# ───────────────────────── ban helpers ─────────────────────────
_banned = None              # set of banned ids, loaded once from the database
_banned_retry_at = 0.0


async def is_banned(user_id: int) -> bool:
    """Authoritative check straight from the database."""
    col = await _col()
    doc = await col.find_one({"_id": user_id}, {"banned": 1})
    return bool(doc and doc.get("banned"))


async def is_banned_fast(user_id: int) -> bool:
    """Memory check used on every update. Fails open if the DB is down."""
    global _banned, _banned_retry_at

    if _banned is None:
        if time.monotonic() < _banned_retry_at:
            return False
        try:
            col = await _col()
            ids = set()
            async for doc in col.find({"banned": True}, {"_id": 1}):
                ids.add(doc["_id"])
            _banned = ids
        except Exception:
            _banned_retry_at = time.monotonic() + 30
            log.warning("could not load banned users", exc_info=True)
            return False

    return user_id in _banned


async def ban_user(user_id: int, by: int) -> None:
    col = await _col()
    await col.update_one(
        {"_id": user_id},
        {
            "$set": {"banned": True, "banned_at": _now(), "banned_by": by},
            "$setOnInsert": {"started": False},
        },
        upsert=True,
    )
    if _banned is not None:
        _banned.add(user_id)


async def unban_user(user_id: int) -> None:
    col = await _col()
    await col.update_one(
        {"_id": user_id},
        {
            "$set": {"banned": False},
            "$unset": {"banned_at": "", "banned_by": ""},
        },
    )
    if _banned is not None:
        _banned.discard(user_id)


# ───────────────────────── counters / iteration ─────────────────────────
async def count_started() -> int:
    col = await _col()
    return await col.count_documents({"started": True})


async def count_users(exclude_banned: bool = True) -> int:
    col = await _col()
    query = {"banned": {"$ne": True}} if exclude_banned else {}
    return await col.count_documents(query)


async def iter_user_ids(exclude_banned: bool = True):
    """Stream user ids one by one (never loads the whole list in memory)."""
    col = await _col()
    query = {"banned": {"$ne": True}} if exclude_banned else {}
    async for doc in col.find(query, {"_id": 1}):
        yield doc["_id"]


# ───────────────────────── log channel ─────────────────────────
async def _log_new_user(client, user_id: int) -> None:
    """Send the stored profile to LOG_CHANNEL (once per user)."""
    if not LOG_CHANNEL:
        return

    doc = await get_user(user_id)
    if not doc:
        return

    first = html.escape(doc.get("first_name") or "—")
    last = html.escape(doc.get("last_name") or "—")
    username = doc.get("username")
    username_text = f"@{html.escape(username)}" if username else "—"
    joined = doc.get("joined_at") or _now()

    text = (
        "🆕 <b>New user</b>\n\n"
        f"👤 <b>First name:</b> {first}\n"
        f"👥 <b>Last name:</b> {last}\n"
        f"🆔 <b>User ID:</b> <code>{doc['_id']}</code>\n"
        f"🔗 <b>Username:</b> {username_text}\n"
        f"📅 <b>Joined:</b> {joined:%Y-%m-%d %H:%M} UTC"
    )

    await client.send_message(
        LOG_CHANNEL,
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ───────────────────────── global tracker (group -1) ─────────────────────────
_START_RE = re.compile(r"^/start(?:@(\w+))?(?:\s|$)", re.IGNORECASE)


async def _track(client, user, message=None) -> None:
    if not user or user.is_bot:
        return

    touch(user.id)

    try:
        if await track_user(user):
            try:
                await _log_new_user(client, user.id)
            except Exception:
                log.warning("could not send to LOG_CHANNEL", exc_info=True)

        if message is not None and message.text:
            match = _START_RE.match(message.text)
            if match:
                target = match.group(1)
                me = getattr(client, "me", None)
                my_name = (getattr(me, "username", None) or "").lower()
                if not target or target.lower() == my_name:
                    await mark_started(user.id)
    except Exception:
        # The bot must keep working even if the database is unreachable.
        log.warning("user tracking failed", exc_info=True)


async def _track_message(client, message):
    await _track(client, message.from_user, message)


async def _track_callback(client, query):
    await _track(client, query.from_user)


def register_user_info(app):
    """Track every user who talks to the bot.

    Runs in handler group -1 (before all commands, after the ban gate at
    group -2) and never stops propagation, so no other handler is affected.
    """
    app.add_handler(MessageHandler(_track_message, filters.all), group=-1)
    app.add_handler(CallbackQueryHandler(_track_callback), group=-1)
