
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
from callback import register_callbacks


# Pyrogram bot
app = Client(
    "toolkit_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Register existing command handlers
register_start(app)
register_qrcode(app)
register_tts(app)
register_callbacks(app)
register_text(app)
register_menu(app)


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

    bot_thread = threading.Thread(
        target=run_bot,
        daemon=True
    )
    bot_thread.start()

    print(f"Starting health server on port {port}...")

    web_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )
