import logging

from pyrogram import filters

from config import STATS_VIDEO, LIVE_WINDOW
from database import user_info
from task_queue import task_queue

log = logging.getLogger(__name__)

_TINY = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
)


def tiny(text: str) -> str:
    """Convert a-z to tiny small-caps letters (A-Z are lower-cased first)."""
    return text.lower().translate(_TINY)


def build_stats_text(live, started, snap) -> str:
    started_text = str(started) if started is not None else tiny("unavailable")

    lines = [
        "📊 " + tiny("bot statistics"),
        "",
        f"🟢 {tiny('live users')} : {live}",
        f"🚀 {tiny('total users (started)')} : {started_text}",
        "",
        f"⏳ {tiny('tasks pending')} : {snap['waiting']}",
        f"⚙️ {tiny('tasks running')} : {snap['running']}",
        f"✅ {tiny('tasks finished')} : {snap['finished']}",
        f"❌ {tiny('tasks failed')} : {snap['failed']}",
        "",
        f"📦 {tiny('queue full (capacity)')} : {snap['capacity']}",
        f"📋 {tiny('queue have')} : {snap['in_system']}",
        f"🆓 {tiny('queue free')} : {snap['free']}",
    ]
    return "\n".join(lines)


async def stats_command(client, message):
    live = user_info.live_users(LIVE_WINDOW)

    try:
        started = await user_info.count_started()
    except Exception:
        log.warning("could not read total users", exc_info=True)
        started = None

    text = build_stats_text(live, started, task_queue.snapshot())

    try:
        await message.reply_video(video=STATS_VIDEO, caption=text)
    except Exception:
        # Bad/unreachable video URL: still show the numbers.
        log.warning("could not send STATS_VIDEO", exc_info=True)
        try:
            await message.reply_text(text)
        except Exception:
            pass


def register_stats(app):
    from pyrogram.handlers import MessageHandler

    app.add_handler(MessageHandler(stats_command, filters.command("stats")))
