"""Automatic language detection for /tts and /translate.

Detects which of the 7 supported languages a text is written in:
en, ja, ko, ru, bn, hi, zh.

How it works (no extra dependency, only aiohttp which the bot already uses)
---------------------------------------------------------------------------
1. The Unicode *script* of every letter is counted. Hangul -> Korean,
   Cyrillic -> Russian, Bengali -> Bangla, Devanagari -> Hindi, kana ->
   Japanese. This is instant and very reliable.
2. Han characters alone can be Chinese OR Japanese (kanji) -> Google's free
   detector decides (falls back to Chinese if it can't be reached).
3. Latin letters can be English, French, Spanish, ... -> Google's detector
   decides, backed by a small local English-word check so short English
   texts are never rejected by a wrong guess. If Google is unreachable the
   local check alone is used, so detection never crashes or blocks.

``detect_language`` never raises. It returns a ``Detection``; ``.key`` is
``None`` when the text is empty / has no letters / is not supported.
"""

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

API_URL = "https://translate.googleapis.com/translate_a/single"
DETECT_TIMEOUT = 4          # seconds for the online check
DETECT_SAMPLE = 300         # characters sent to the online check

SUPPORTED_KEYS = ("en", "ja", "ko", "ru", "bn", "hi", "zh")

# detection reasons
OK = "ok"
NO_TEXT = "no_text"             # no letters at all (digits, emoji, symbols)
UNSUPPORTED = "unsupported"     # a language / script we can't handle


@dataclass(frozen=True)
class Detection:
    key: Optional[str]
    reason: str = OK

    @property
    def supported(self) -> bool:
        return self.key is not None


# ───────────────────────── script counting ─────────────────────────
def _script_of(ch: str) -> Optional[str]:
    """Return the script group of a letter, or None for non-letters."""
    if not ch.isalpha():
        return None
    cp = ord(ch)

    if (
        0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF
        or 0x3130 <= cp <= 0x318F or 0xA960 <= cp <= 0xA97F
        or 0xD7B0 <= cp <= 0xD7FF
    ):
        return "hangul"
    if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF or 0xFF66 <= cp <= 0xFF9F:
        return "kana"
    if (
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
        or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2FA1F
    ):
        return "han"
    if 0x0400 <= cp <= 0x052F:
        return "cyrillic"
    if 0x0980 <= cp <= 0x09FF:
        return "bengali"
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if (
        cp < 0x250 or 0x1E00 <= cp <= 0x1EFF
    ) and unicodedata.name(ch, "").startswith("LATIN"):
        return "latin"
    return "other"


def _count_scripts(text: str) -> dict:
    counts: dict = {}
    for ch in text:
        script = _script_of(ch)
        if script:
            counts[script] = counts.get(script, 0) + 1
    return counts


# ───────────────────────── English check ─────────────────────────
_ENGLISH_WORDS = frozenset(
    """the be to of and a in that have i it for not on with he as you do at
    this but his by from they we say her she or an will my one all would
    there their what so up out if about who get which go me when make can
    like time no just him know take people into year your good some could
    them see other than then now look only come its over think also back
    after use two how our work first well way even new want because any
    these give day most us is are was were am been has had did does hello
    hi hey thanks thank please yes ok okay love how's what's i'm it's don't
    can't won't let here where why very much more really right still""".split()
)
_WORD_RE = re.compile(r"[a-z']+")


def _english_hits(text: str) -> tuple:
    words = _WORD_RE.findall(text.lower())
    hits = sum(1 for w in words if w in _ENGLISH_WORDS)
    return hits, len(words)


def _has_accented_latin(text: str) -> bool:
    return any(ord(ch) > 127 and _script_of(ch) == "latin" for ch in text)


# ───────────────────────── online check ─────────────────────────
async def _google_detect(text: str) -> Optional[str]:
    """Ask Google which language the text is. None when it can't be reached."""
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": "en",
        "dt": "t",
        "q": text[:DETECT_SAMPLE],
    }
    try:
        timeout = aiohttp.ClientTimeout(total=DETECT_TIMEOUT)
        async with aiohttp.ClientSession(
            timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
        ) as http:
            async with http.get(API_URL, params=params) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
        code = data[2]
        if isinstance(code, str) and code:
            return code.lower()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # network error, bad JSON, timeout, ...
        log.debug("online language check failed: %r", exc)
    return None


# ───────────────────────── public API ─────────────────────────
async def detect_language(text: str) -> Detection:
    """Detect the language of ``text``. Never raises."""
    try:
        return await _detect(text or "")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("language detection crashed", exc_info=True)
        return Detection(None, UNSUPPORTED)


async def _detect(text: str) -> Detection:
    counts = _count_scripts(text)
    if not counts:
        return Detection(None, NO_TEXT)

    kana = counts.get("kana", 0)
    han = counts.get("han", 0)
    groups = {
        "cjk": kana + han,
        "hangul": counts.get("hangul", 0),
        "cyrillic": counts.get("cyrillic", 0),
        "bengali": counts.get("bengali", 0),
        "devanagari": counts.get("devanagari", 0),
        "latin": counts.get("latin", 0),
        "other": counts.get("other", 0),
    }
    # The script with the most letters wins (ties keep the order above).
    dominant = max(groups, key=groups.get)

    if dominant == "other":
        return Detection(None, UNSUPPORTED)
    if dominant == "hangul":
        return Detection("ko")
    if dominant == "cyrillic":
        return Detection("ru")
    if dominant == "bengali":
        return Detection("bn")
    if dominant == "devanagari":
        return Detection("hi")

    if dominant == "cjk":
        if kana > 0:
            return Detection("ja")          # kana only exists in Japanese
        code = await _google_detect(text)   # Han only: Chinese or Japanese?
        return Detection("ja" if code and code.startswith("ja") else "zh")

    return await _detect_latin(text)


async def _detect_latin(text: str) -> Detection:
    hits, total = _english_hits(text)
    code = await _google_detect(text)

    if code is not None:
        if code.startswith("en"):
            return Detection("en")
        # Google guessed another language. A short English text is easy to
        # mis-guess, so clear English wording overrides it.
        if hits >= 2 and total and hits / total >= 0.25:
            return Detection("en")
        return Detection(None, UNSUPPORTED)

    # Offline fallback (Google unreachable): decide locally.
    if hits > 0:
        return Detection("en")
    if _has_accented_latin(text):
        return Detection(None, UNSUPPORTED)   # looks like French/Spanish/...
    return Detection("en")                    # plain ASCII: assume English
