from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN
from handle.menu import register_menu
from handle.start import register_start
from handle.qrcode import register_qrcode
from handle.tts import register_tts
from handle.text import register_text
from callback import register_callbacks


app = Client(
    "toolkit_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)


# Register command handlers
register_start(app)
register_qrcode(app)
register_tts(app)
register_callbacks(app)
register_text(app)
register_menu(app)


if __name__ == "__main__":
    print("Bot was deployed...")
    app.run()
  
