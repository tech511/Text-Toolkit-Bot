import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

MAX_TEXT_LENGTH = 500
COOLDOWN = 120
MAX_RUNNING = 2
MAX_WAITING = 3
TTS_TIMEOUT = 20

START_TEXT = """
ʜᴇʏ, {first_name}! ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ᴛᴇxᴛ ᴛᴏᴏʟ ᴋɪᴛ ʙᴏᴛ 🌍

ɪᴛ'ꜱ ᴀ ʟɪɢʜᴛᴡᴇɪɢʜᴛ ᴍᴜʟᴛɪ-ᴛᴏᴏʟ ʙᴏᴛ ʙᴜɪʟᴛ ᴛᴏ ᴍᴀᴋᴇ ᴇᴠᴇʀʏᴅᴀʏ ᴛᴀꜱᴋꜱ ᴇᴀꜱɪᴇʀ.⚡
ᴇxᴘʟᴏʀᴇ ᴛʜᴇ ꜰᴜᴛᴜʀᴇꜱ ʙʏ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ. 🚀

ᴘᴏᴡᴇʀᴇᴅ ʙʏ:- @Bot_Arena_Official
"""

HELP_CAPTION = """
✦ ʜᴏᴡ ᴛᴏ ɢᴇᴛ ꜱᴛᴀʀᴛᴇᴅ?

ᴛᴀᴘ ᴀɴʏ ꜰᴇᴀᴛᴜʀᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ
ᴛᴏ ʟᴇᴀʀɴ ʜᴏᴡ ɪᴛ ᴡᴏʀᴋꜱ. 💡

ɴᴇᴇᴅ ʜᴇʟᴘ? ᴄʜᴏᴏꜱᴇ ᴀ ꜰᴇᴀᴛᴜʀᴇ
ᴀɴᴅ ꜰᴏʟʟᴏᴡ ᴛʜᴇ ɪɴꜱᴛʀᴜᴄᴛɪᴏɴꜱ.
"""

# QR Code guide
QRCODE_CAPTION = """
📱 ǫʀ ᴄᴏᴅᴇ ɢᴇɴᴇʀᴀᴛᴏʀ

ᴛᴜʀɴ ʏᴏᴜʀ ᴛᴇxᴛ ᴏʀ ʟɪɴᴋ
ɪɴᴛᴏ ᴀ ꜱᴄᴀɴɴᴀʙʟᴇ ǫʀ ᴄᴏᴅᴇ! ✨

📝 ʜᴏᴡ ᴛᴏ ᴜꜱᴇ:
➊ ꜱᴇɴᴅ ᴀɴʏ ᴛᴇxᴛ ᴏʀ ᴜʀʟ.
➋ ʀᴇᴘʟʏ ᴛᴏ ɪᴛ ᴡɪᴛʜ /qrcode.
➌ ɢᴇᴛ ʏᴏᴜʀ ǫʀ ᴄᴏᴅᴇ ɪɴꜱᴛᴀɴᴛʟʏ. 🚀

💡 ꜱᴄᴀɴ ɪᴛ ᴀɴᴅ ꜱʜᴀʀᴇ!
"""

# Text Toolkit guide
TEXT_TOOLKIT_CAPTION = """
📝 ᴛᴇxᴛ ᴛᴏᴏʟᴋɪᴛ

ᴍᴀᴋᴇ ʏᴏᴜʀ ᴛᴇxᴛ ᴄʟᴇᴀɴ,
ꜱᴍᴀʀᴛ, ᴀɴᴅ ᴇᴀꜱʏ ᴛᴏ ᴍᴀɴᴀɢᴇ. ⚡

🛠️ ᴄᴏᴍᴍᴀɴᴅꜱ:
/count — ᴄᴏᴜɴᴛ ᴡᴏʀᴅꜱ & ᴄʜᴀʀᴀᴄᴛᴇʀꜱ
/upper — ᴄᴏɴᴠᴇʀᴛ ᴛᴏ ᴜᴘᴘᴇʀᴄᴀꜱᴇ
/lower — ᴄᴏɴᴠᴇʀᴛ ᴛᴏ ʟᴏᴡᴇʀᴄᴀꜱᴇ
/reverse — ʀᴇᴠᴇʀꜱᴇ ᴛʜᴇ ᴛᴇxᴛ
/clean — ʀᴇᴍᴏᴠᴇ ᴇxᴛʀᴀ ꜱᴘᴀᴄᴇꜱ
/bold — ᴍᴀᴋᴇ ᴛᴇxᴛ ʙᴏʟᴅ
/italic — ᴍᴀᴋᴇ ᴛᴇxᴛ ɪᴛᴀʟɪᴄ
/mono — ᴍᴏɴᴏꜱᴘᴀᴄᴇ ꜰᴏʀᴍᴀᴛ

👇 ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴛᴇxᴛ ᴍᴇꜱꜱᴀɢᴇ
ᴡɪᴛʜ ᴛʜᴇ ᴄᴏᴍᴍᴀɴᴅ ʏᴏᴜ ᴡᴀɴᴛ.
"""

# Text-to-Speech guide
TEXT_TO_SPEECH_CAPTION = """
🔊 ᴛᴇxᴛ ᴛᴏ ꜱᴘᴇᴇᴄʜ

ᴛᴜʀɴ ʏᴏᴜʀ ᴛᴇxᴛ ɪɴᴛᴏ ꜱᴘᴇᴇᴄʜ
ɪɴ 7 ʟᴀɴɢᴜᴀɢᴇꜱ ᴀɴᴅ ʟɪꜱᴛᴇɴ! 🎧

🎙️ ʜᴏᴡ ᴛᴏ ᴜꜱᴇ:
➊ ꜱᴇɴᴅ ᴀɴʏ ᴛᴇxᴛ.
➋ ʀᴇᴘʟʏ ᴛᴏ ɪᴛ ᴡɪᴛʜ /tts.
➌ ᴘɪᴄᴋ ʟᴀɴɢᴜᴀɢᴇ ᴀɴᴅ ᴠᴏɪᴄᴇ. 🎵

🌍 ʟᴀɴɢᴜᴀɢᴇꜱ:
ᴇɴɢʟɪꜱʜ • ᴊᴀᴘᴀɴᴇꜱᴇ • ᴋᴏʀᴇᴀɴ • ʀᴜꜱꜱɪᴀɴ
ʙᴀɴɢʟᴀ • ʜɪɴᴅɪ • ᴄʜɪɴᴇꜱᴇ

📌 ʟɪᴍɪᴛꜱ:
• ᴍᴀx 500 ᴄʜᴀʀᴀᴄᴛᴇʀꜱ
• 120-ꜱᴇᴄᴏɴᴅ ᴘᴇʀ-ᴜꜱᴇʀ ᴄᴏᴏʟᴅᴏᴡɴ
• 2 ᴀᴄᴛɪᴠᴇ ɢᴇɴᴇʀᴀᴛɪᴏɴꜱ
• 3 ᴡᴀɪᴛɪɴɢ ᴊᴏʙꜱ

✨ ᴛᴇxᴛ ɪɴ, ᴠᴏɪᴄᴇ ᴏᴜᴛ!
"""

# Videos — replace with your own video URLs
HELP_VIDEO = "YOUR_HELP_VIDEO_URL"
QRCODE_VIDEO = "https://graph.org/file/218f412cc841c4f8945b9-4223b8eef03e22ff13.mp4"
TEXT_TOOLKIT_VIDEO = "https://graph.org/file/218f412cc841c4f8945b9-4223b8eef03e22ff13.mp4"
TEXT_TO_SPEECH_VIDEO = "https://graph.org/file/218f412cc841c4f8945b9-4223b8eef03e22ff13.mp4"

ABOUT_CAPTION = """
➤ ɴᴀᴍᴇ : ᴛᴇxᴛ ᴛᴏᴏʟᴋɪᴛ ʙᴏᴛ
➤ ʟᴀɴɢᴜᴀɢᴇ : ᴘʏᴛʜᴏɴ 🐍
➤ ʟɪʙʀᴀʀʏ : ᴘʏʀᴏɢʀᴀᴍ ⚡
➤ ᴅᴇᴠᴇʟᴏᴘᴇʀ : @DA_R_KS_I_DE
✦ ғᴇᴀᴛᴜʀᴇꜱ
  • Qʀ ᴄᴏᴅᴇ ɢᴇɴᴇʀᴀᴛᴏʀ
  • ᴛᴇxᴛ ᴛᴏᴏʟᴋɪᴛ
  • ᴛᴇxᴛ ᴛᴏ ꜱᴘᴇᴇᴄʜ
"""
# Replace these with your COMPLETE video URLs.
START_VIDEO = "https://graph.org/file/218f412cc841c4f8945b9-4223b8eef03e22ff13.mp4"
HELP_VIDEO = "https://graph.org/file/218f412cc841c4f8945b9-4223b8eef03e22ff13.mp4"
ABOUT_VIDEO = "https://graph.org/file/218f412cc841c4f8945b9-4223b8eef03e22ff13.mp4"

MORE_BOTS_URL = "https://t.me/AniWorld_Bots_Hub"
OWNER_URL = "https://t.me/DA_R_KS_I_DE"


# ───────────────────────── Owner ─────────────────────────
# Your numeric Telegram user id (only this account can use /ban, /unban,
# /broadcast). Set it in the environment: OWNER_ID=123456789
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

# Owner @username shown on the "you are banned" button.
# Defaults to the username inside OWNER_URL.
OWNER_USERNAME = (
    os.getenv("OWNER_USERNAME")
    or OWNER_URL.rstrip("/").split("/")[-1]
).lstrip("@")


# ───────────────────────── MongoDB ─────────────────────────
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "text_toolkit_bot")

# Channel that receives one message the first time a new user is saved.
# Use a numeric id (-100xxxxxxxxxx) or a public @username. Empty = disabled.
_log_channel = (os.getenv("LOG_CHANNEL") or "").strip()
if _log_channel.lstrip("-").isdigit():
    LOG_CHANNEL = int(_log_channel)
else:
    LOG_CHANNEL = _log_channel or None


# ───────────────────────── Queue (task_queue.py) ─────────────────────────
# MAX_RUNNING / MAX_WAITING are defined at the top of this file.
MAX_USER_TASKS = 2            # tasks one user may have running + waiting
QUEUE_TASK_TIMEOUT = 60       # default hard limit for a single job (seconds)

# Per-command cooldowns (seconds). TTS uses COOLDOWN from the top of the file.
QRCODE_COOLDOWN = 3
TRANSLATE_COOLDOWN = 5
TXTFILE_COOLDOWN = 3

TRANSLATE_TIMEOUT = 15        # seconds for the translation request
TXTFILE_MAX_LENGTH = 20000    # max characters for /txtfile


# ───────────────────────── Stats ─────────────────────────
STATS_VIDEO = "https://graph.org/file/218f412cc841c4f8945b9-4223b8eef03e22ff13.mp4"
LIVE_WINDOW = 300             # a user counts as "live" if active in the last N seconds


# ───────────────────────── Ban ─────────────────────────
BAN_TEXT = (
    "🚫 ʏᴏᴜ ᴀʀᴇ ʙᴀɴɴᴇᴅ ᴛᴏ ᴜꜱᴇ ᴛʜɪꜱ ʙᴏᴛ,\n"
    "ᴄᴏɴᴛᴀᴄᴛ ᴏᴡɴᴇʀ ᴛᴏ ꜱᴏʟᴠᴇ ᴛʜᴇ ɪꜱꜱᴜᴇ."
)
UNBAN_TEXT = "✅ ʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴜɴʙᴀɴɴᴇᴅ. ʏᴏᴜ ᴄᴀɴ ᴜꜱᴇ ᴛʜᴇ ʙᴏᴛ ᴀɢᴀɪɴ!"
