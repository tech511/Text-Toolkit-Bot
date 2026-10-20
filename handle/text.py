"""Button-based Unicode text styles for the /fronts command.

This replaces the old /count, /upper, /lower, /reverse, /clean,
/bold, /italic and /mono commands. The rest of the bot can stay unchanged:
app.py may continue calling register_text(app).
"""

import time
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MAX_TEXT_LENGTH = 4000
COOLDOWN = 2
PAGE_SIZE = 8

last_request = {}
# (chat_id, menu_message_id) -> {user_id, text, created_at}
fronts_sessions = {}

# Unicode Mathematical Alphanumeric Symbols. Characters not present in a
# style's mapping (emoji, punctuation, Bangla, etc.) are preserved unchanged.
STYLE_RANGES = {
    "bold": (0x1D400, 0x1D41A, 0x1D7CE),
    "italic": (0x1D434, 0x1D44E, None),
    "bold_italic": (0x1D468, 0x1D482, None),
    "sans": (0x1D5A0, 0x1D5BA, 0x1D7E2),
    "sans_bold": (0x1D5D4, 0x1D5EE, 0x1D7EC),
    "sans_italic": (0x1D608, 0x1D622, None),
    "sans_bold_italic": (0x1D63C, 0x1D656, None),
    "monospace": (0x1D670, 0x1D68A, 0x1D7F6),
    "double_struck": (0x1D538, 0x1D552, 0x1D7D8),
    "script": (0x1D49C, 0x1D4B6, None),
    "bold_script": (0x1D4D0, 0x1D4EA, None),
    "fraktur": (0x1D504, 0x1D51E, None),
    "bold_fraktur": (0x1D56C, 0x1D586, None),
}

# Letterlike-symbol exceptions in the Unicode math alphabets.
SPECIAL_LETTERS = {
    "italic": {"h": "ℎ"},
    "script": {
        "B": "ℬ", "E": "ℰ", "F": "ℱ", "H": "ℋ", "I": "ℐ",
        "L": "ℒ", "M": "ℳ", "R": "ℛ", "e": "ℯ", "g": "ℊ",
        "o": "ℴ",
    },
    "fraktur": {"C": "ℭ", "H": "ℌ", "I": "ℑ", "R": "ℜ", "Z": "ℨ"},
    "double_struck": {
        "C": "ℂ", "H": "ℍ", "N": "ℕ", "P": "ℙ", "Q": "ℚ",
        "R": "ℝ", "Z": "ℤ",
    },
}

STYLE_LABELS = {
    "bold": "𝗕𝗼𝗹𝗱",
    "italic": "𝘐𝘵𝘢𝘭𝘪𝘤",
    "bold_italic": "𝙱𝙤𝙡𝙙 𝘐𝘵𝘢𝘭𝘪𝘤",
    "sans": "Sans Serif",
    "sans_bold": "𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱",
    "sans_italic": "𝘚𝘢𝘯𝘴 𝘐𝘵𝘢𝘭𝘪𝘤",
    "sans_bold_italic": "𝙎𝙖𝙣𝙨 𝘽𝙤𝙡𝙙 𝙄𝙩𝙖𝙡𝙞𝙘",
    "monospace": "𝙼𝚘𝚗𝚘𝚜𝚙𝚊𝚌𝚎",
    "double_struck": "𝔻𝕠𝕦𝕓𝕝𝕖 𝕊𝕥𝕣𝕦𝕔𝕜",
    "script": "𝒮𝒸𝓇𝒾𝓅𝓉",
    "bold_script": "𝓑𝓸𝓵𝓭 𝓢𝓬𝓻𝓲𝓹𝓽",
    "fraktur": "𝔉𝔯𝔞𝔨𝔱𝔲𝔯",
    "bold_fraktur": "𝕭𝖔𝖑𝖉 𝕱𝖗𝖆𝖐𝖙𝖚𝖗",
    "circled": "Ⓒⓘⓡⓒⓛⓔⓓ",
    "squared": "🅂🅀🅄🄰🅁🄴🄳",
    "fullwidth": "Ｆｕｌｌｗｉｄｔｈ",
    "small_caps": "Sᴍᴀʟʟ Cᴀᴘs",
    "upside_down": "Upside Down",
}

STYLE_ORDER = list(STYLE_LABELS)

# Circled Latin letters are a separate Unicode block. Uppercase and lowercase
# letters have different ranges; unsupported characters are left untouched.
CIRCLED_UPPER = {chr(ord("A") + i): chr(0x24B6 + i) for i in range(26)}
CIRCLED_LOWER = {chr(ord("a") + i): chr(0x24D0 + i) for i in range(26)}
CIRCLED_DIGITS = {str(i): chr(0x2460 + i - 1) for i in range(1, 10)}
CIRCLED_DIGITS["0"] = "⓪"

# Squared letter forms only exist for a subset of Latin characters.
SQUARED_MAP = {
    **{chr(ord("A") + i): chr(0x1F130 + i) for i in range(26)},
    **{chr(ord("a") + i): chr(0x1F130 + i) for i in range(26)},
}

SMALL_CAPS = {
    "a":"ᴀ", "b":"ʙ", "c":"ᴄ", "d":"ᴅ", "e":"ᴇ", "f":"ꜰ",
    "g":"ɢ", "h":"ʜ", "i":"ɪ", "j":"ᴊ", "k":"ᴋ", "l":"ʟ",
    "m":"ᴍ", "n":"ɴ", "o":"ᴏ", "p":"ᴘ", "q":"ǫ", "r":"ʀ",
    "s":"s", "t":"ᴛ", "u":"ᴜ", "v":"ᴠ", "w":"ᴡ", "x":"x",
    "y":"ʏ", "z":"ᴢ",
}
UPSIDE_DOWN = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.,?!'()[]{}<>_&",
    "ɐqɔpǝɟƃɥᴉɾʞןɯuodbɹsʇnʌʍxʎz∀𐐒ƆᗡƎℲפHIſ⋊˥WNOԀΌᴚS⊥∩ΛMX⅄Z0ƖᄅƐㄣϛ9ㄥ86'˙¿¡,)(][}{><‾⅋",
)


def style_text(text: str, style: str) -> str:
    """Convert supported Latin letters/digits while preserving other text."""
    if style == "fullwidth":
        return "".join(
            "　" if ch == " " else chr(ord(ch) + 0xFEE0)
            if "!" <= ch <= "~" else ch
            for ch in text
        )
    if style == "circled":
        mapping = {**CIRCLED_UPPER, **CIRCLED_LOWER, **CIRCLED_DIGITS}
        return "".join(mapping.get(ch, ch) for ch in text)
    if style == "squared":
        return "".join(SQUARED_MAP.get(ch, ch) for ch in text)
    if style == "small_caps":
        return "".join(SMALL_CAPS.get(ch.lower(), ch) if ch.isalpha() and ch.lower() in SMALL_CAPS else ch for ch in text)
    if style == "upside_down":
        return text.translate(UPSIDE_DOWN)[::-1]

    ranges = STYLE_RANGES.get(style)
    special = SPECIAL_LETTERS.get(style, {})
    result = []
    for ch in text:
        if ch in special:
            result.append(special[ch])
        elif ranges and "A" <= ch <= "Z":
            result.append(chr(ranges[0] + ord(ch) - ord("A")))
        elif ranges and "a" <= ch <= "z":
            result.append(chr(ranges[1] + ord(ch) - ord("a")))
        elif ranges and ranges[2] is not None and "0" <= ch <= "9":
            result.append(chr(ranges[2] + ord(ch) - ord("0")))
        else:
            result.append(ch)
    return "".join(result)


def fronts_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    keys = STYLE_ORDER[start:start + PAGE_SIZE]
    rows = []
    for i in range(0, len(keys), 2):
        rows.append([
            InlineKeyboardButton(STYLE_LABELS[keys[i]], callback_data=f"fronts:style:{keys[i]}"),
            *([InlineKeyboardButton(STYLE_LABELS[keys[i + 1]], callback_data=f"fronts:style:{keys[i + 1]}")] if i + 1 < len(keys) else []),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"fronts:page:{page - 1}"))
    if start + PAGE_SIZE < len(STYLE_ORDER):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"fronts:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="fronts:close")])
    return InlineKeyboardMarkup(rows)


def register_text(app):
    """Register /fronts and its inline-button callbacks."""

    @app.on_message(filters.command("fronts"))
    async def fronts_command(client, message):
        user = message.from_user
        source = message.reply_to_message
        if not user:
            return
        if not source or not source.text:
            await message.reply_text(
                "📝 Send a text message, then reply to it with /fronts."
            )
            return

        text = source.text.strip()
        if not text:
            await message.reply_text("📝 The replied message is empty.")
            return
        if len(text) > MAX_TEXT_LENGTH:
            await message.reply_text(
                f"⚠️ Text is too long! Please use {MAX_TEXT_LENGTH} characters or fewer."
            )
            return

        user_id = user.id
        now = time.monotonic()
        # Drop abandoned menus so this in-memory cache stays bounded.
        for old_key, old_session in list(fronts_sessions.items()):
            if now - old_session["created_at"] > 1800:
                fronts_sessions.pop(old_key, None)
        elapsed = now - last_request.get(user_id, 0)
        if elapsed < COOLDOWN:
            remaining = max(1, int(COOLDOWN - elapsed + 0.99))
            await message.reply_text(
                f"⏳ Please wait {remaining} second(s) before using /fronts again."
            )
            return
        last_request[user_id] = now

        menu = await message.reply_text(
            "🔤 Choose a font style\n\n"
            "Tap any button to get a styled version of your text.\n"
            "Some styles work best with English letters and numbers.",
            reply_markup=fronts_keyboard(0),
        )
        fronts_sessions[(menu.chat.id, menu.id)] = {
            "user_id": user_id,
            "text": text,
            "created_at": now,
        }

    @app.on_callback_query(filters.regex(r"^fronts:(page:\d+|style:[a-z_]+|close)$"))
    async def fronts_callback(client, query):
        if not query.message or not query.from_user:
            await query.answer("This menu is unavailable.", show_alert=True)
            return

        key = (query.message.chat.id, query.message.id)
        session = fronts_sessions.get(key)
        if not session:
            await query.answer("This menu has expired. Send /fronts again.", show_alert=True)
            return
        if session["user_id"] != query.from_user.id:
            await query.answer("This font menu belongs to another user.", show_alert=True)
            return

        data = query.data
        if data == "fronts:close":
            fronts_sessions.pop(key, None)
            try:
                await query.message.delete()
            except Exception:
                pass
            await query.answer("Closed.")
            return

        if data.startswith("fronts:page:"):
            page = int(data.rsplit(":", 1)[1])
            await query.message.edit_reply_markup(reply_markup=fronts_keyboard(page))
            await query.answer()
            return

        style = data.rsplit(":", 1)[1]
        if style not in STYLE_LABELS:
            await query.answer("Unknown style.", show_alert=True)
            return

        result = style_text(session["text"], style)
        try:
            output = f"{STYLE_LABELS[style]}\n\n{result}"
            # Telegram limits message length; supplementary Unicode font
            # characters can count as two UTF-16 units each.
            if len(output.encode("utf-16-le")) // 2 > 4096:
                await query.answer(
                    "This style makes the text too long for Telegram. Try a shorter text.",
                    show_alert=True,
                )
                return
            await query.message.reply_text(
                output,
                disable_web_page_preview=True,
            )
            await query.answer("Style generated!")
        except Exception:
            await query.answer("Couldn't send the styled text.", show_alert=True)
