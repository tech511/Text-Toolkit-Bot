import logging
import os
import threading
from flask import Flask
from pyrogram import Client

from config import API_ID, API_HASH, BOT_TOKEN
from handle.menu import register_menu
from handle.start import register_start
from handle.qrcode import register_qrcode
from handle.tts import register_tts
from handle.text import register_text
from handle.translate import register_translate
from handle.txtfile import register_txtfile
from handle.stats import register_stats
from handle.ban import register_ban
from handle.unban import register_unban
from handle.broadcast import register_broadcast
from database.user_info import register_user_info
from callback import register_callbacks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)


# Pyrogram bot
app = Client(
    "toolkit_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Global gates (run before every command): banned users are blocked first
# (group -2), then every user is saved/tracked (group -1).
register_ban(app)
register_user_info(app)

# Register existing command handlers
register_start(app)
register_qrcode(app)
register_tts(app)
register_callbacks(app)
register_text(app)
register_menu(app)

# New command handlers
register_translate(app)
register_txtfile(app)
register_stats(app)
register_unban(app)
register_broadcast(app)

# HTTP health-check server
web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "Text Toolkit Bot is running!", 200


@web_app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_bot():
    print("Starting Pyrogram bot...")
    app.run()



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    # Run the HTTP health server in a background thread.
    web_thread = threading.Thread(
        target=lambda: web_app.run(
            host="0.0.0.0",
            port=port,
            use_reloader=False
        ),
        daemon=True
    )
    web_thread.start()

    # Keep Pyrogram on the main thread.
    print("Starting Pyrogram bot...")
    app.run()

