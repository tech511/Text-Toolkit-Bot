"""/broadcast — owner replies to any post with /broadcast.

* The post is COPIED to every user (no "forwarded from" header) and keeps
  its inline buttons.
* A progress bar is edited every 2 seconds while sending.
* A final report shows how many users received the post and how many did not.
* Only the owner can start or stop it; only one broadcast runs at a time.
"""

import asyncio
import logging
import time

from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    ChatWriteForbidden,
    FloodWait,
    InputUserDeactivated,
    MessageNotModified,
    PeerIdInvalid,
    RPCError,
    UserIsBlocked,
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import user_info
from handle.ban import is_owner

log = logging.getLogger(__name__)

CHUNK_SIZE = 20          # users sent in parallel (~20 msgs/second)
PROGRESS_EVERY = 2       # seconds between progress-bar updates
MAX_FLOOD_RETRIES = 3
BAR_WIDTH = 12

# users who blocked the bot / deleted their account
UNREACHABLE = (UserIsBlocked, InputUserDeactivated, PeerIdInvalid, ChatWriteForbidden)

_active = False          # only one broadcast at a time
_state = None            # live counters of the running broadcast
_tasks = set()


def stop_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏹ Stop broadcast", callback_data="bc:stop")]]
    )


def progress_bar(done, total):
    percent = 100 if total <= 0 else min(100, int(done * 100 / total))
    filled = BAR_WIDTH * percent // 100
    return "█" * filled + "░" * (BAR_WIDTH - filled), percent


def progress_text(state, title):
    done = state["ok"] + state["blocked"] + state["failed"]
    total = max(state["total"], 1)
    bar, percent = progress_bar(done, state["total"])
    return (
        f"{title}\n\n"
        f"[{bar}] {percent}%\n\n"
        f"👥 Total users: {state['total']}\n"
        f"📨 Processed: {min(done, total)}/{state['total']}\n"
        f"✅ Received: {state['ok']}\n"
        f"❌ Not received: {state['blocked'] + state['failed']}"
    )


def final_text(state, title, seconds):
    not_received = state["blocked"] + state["failed"]
    return (
        f"{title}\n\n"
        f"👥 Total users: {state['total']}\n"
        f"✅ Received the post: {state['ok']}\n"
        f"❌ Not received / failed: {not_received}\n"
        f"   ├ 🚫 Blocked the bot / deleted account: {state['blocked']}\n"
        f"   └ ⚠️ Other errors: {state['failed']}\n"
        f"⏱ Time: {seconds}s"
    )


async def edit_status(message, text, markup=None):
    """Edit the progress message; never raises."""
    try:
        await message.edit_text(
            text, reply_markup=markup, parse_mode=ParseMode.DISABLED
        )
    except MessageNotModified:
        pass
    except FloodWait as exc:
        await asyncio.sleep(min(exc.value, 10))
    except Exception:
        pass


async def send_one(client, user_id, source):
    """Copy the post to one user. Returns 'ok', 'blocked' or 'failed'."""
    for _ in range(MAX_FLOOD_RETRIES):
        try:
            if source.media_group_id:
                await client.copy_media_group(user_id, source.chat.id, source.id)
            else:
                await source.copy(user_id)  # copy = no forward header, keeps buttons
            return "ok"
        except FloodWait as exc:
            await asyncio.sleep(min(exc.value, 60) + 1)
        except UNREACHABLE:
            return "blocked"
        except RPCError:
            return "failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("broadcast send error", exc_info=True)
            return "failed"
    return "failed"


async def flush(client, batch, source, state):
    started = time.monotonic()
    results = await asyncio.gather(
        *(send_one(client, uid, source) for uid in batch),
        return_exceptions=True,
    )
    for result in results:
        if result == "ok":
            state["ok"] += 1
        elif result == "blocked":
            state["blocked"] += 1
        else:
            state["failed"] += 1

    # Stay under Telegram's ~30 messages/second limit.
    spent = time.monotonic() - started
    if spent < 1.0:
        await asyncio.sleep(1.0 - spent)


async def sender(client, source, state):
    batch = []
    async for user_id in user_info.iter_user_ids(exclude_banned=True):
        if state["stop"]:
            break
        batch.append(user_id)
        if len(batch) >= CHUNK_SIZE:
            await flush(client, batch, source, state)
            batch = []
    if batch and not state["stop"]:
        await flush(client, batch, source, state)


async def run_broadcast(client, status, source, total):
    global _active, _state

    state = {"total": total, "ok": 0, "blocked": 0, "failed": 0, "stop": False}
    _state = state
    started = time.monotonic()
    error = None

    try:
        worker = asyncio.create_task(sender(client, source, state))

        # Refresh the progress bar every 2 seconds until the sender is done.
        while not worker.done():
            await asyncio.wait({worker}, timeout=PROGRESS_EVERY)
            if worker.done():
                break
            await edit_status(
                status, progress_text(state, "📢 Broadcasting..."), stop_markup()
            )

        try:
            worker.result()
        except Exception as exc:  # e.g. the database went away mid-way
            error = exc
            log.warning("broadcast interrupted", exc_info=True)

        state["total"] = max(state["total"], state["ok"] + state["blocked"] + state["failed"])
        seconds = int(time.monotonic() - started)

        if error is not None:
            title = "⚠️ Broadcast interrupted (database error)"
        elif state["stop"]:
            title = "⏹ Broadcast stopped"
        else:
            title = "✅ Broadcast completed"

        await edit_status(status, final_text(state, title, seconds))

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("broadcast crashed")
        await edit_status(status, "❌ Broadcast crashed. Check the logs.")
    finally:
        _active = False
        _state = None


def register_broadcast(app):

    @app.on_message(filters.command("broadcast"))
    async def broadcast_command(client, message):
        global _active

        user = message.from_user
        if not user:
            return

        if not is_owner(user):
            await message.reply_text("⛔ This command is only for the bot owner.")
            return

        source = message.reply_to_message
        if not source or source.empty or source.service:
            await message.reply_text(
                "📢 Reply to the post you want to broadcast with /broadcast.\n"
                "Forwarded posts and posts with buttons are supported."
            )
            return

        if _active:
            await message.reply_text("⏳ A broadcast is already running.")
            return

        _active = True  # set before any await so two commands can't race
        try:
            total = await user_info.count_users(exclude_banned=True)
            if total == 0:
                _active = False
                await message.reply_text("ℹ️ There are no users to broadcast to yet.")
                return

            status = await message.reply_text(
                f"📢 Starting broadcast to {total} users...",
                reply_markup=stop_markup(),
                parse_mode=ParseMode.DISABLED,
            )
            task = asyncio.create_task(run_broadcast(client, status, source, total))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)

        except Exception:
            _active = False
            log.warning("could not start broadcast", exc_info=True)
            await message.reply_text("❌ Couldn't start the broadcast (database error).")

    @app.on_callback_query(filters.regex(r"^bc:stop$"))
    async def broadcast_stop(client, query):
        if not is_owner(query.from_user):
            await query.answer("Only the owner can do this.", show_alert=True)
            return

        if _state is None:
            await query.answer("No broadcast is running.", show_alert=True)
            return

        _state["stop"] = True
        await query.answer("Stopping after the current batch...")
