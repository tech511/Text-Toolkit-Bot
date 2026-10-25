from pyrogram import filters
from pyrogram.handlers import MessageHandler

from config import HELP_CAPTION, HELP_VIDEO
from buttons import help_button


async def help_command(client, message):

    await message.reply_video(
        video=HELP_VIDEO,
        caption=HELP_CAPTION,
        reply_markup=help_button()
    )


def register_menu(app):

    app.add_handler(
        MessageHandler(
            help_command,
            filters.command("help")
        )
    )
