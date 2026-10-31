"""Reaction helpers: Telegram's allowed emoji set, parsing user input, and the react() action."""
from __future__ import annotations

import asyncio
import logging
import random

from .tg import TelegramError

log = logging.getLogger("reactions")

VS16 = "\ufe0f"  # emoji variation selector — Telegram's list is written without it
ZWJ = "\u200d"

# Emoji Telegram allows for bot reactions (Bot API: ReactionTypeEmoji).
_ALLOWED_RAW = (
    "👍 👎 ❤ 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 🤣 ⚡ "
    "🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 ☃ 💅 🤪 "
    "🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡"
).split()

ALLOWED: tuple[str, ...] = tuple(dict.fromkeys(e.replace(VS16, "") for e in _ALLOWED_RAW))
_BY_LENGTH = sorted(ALLOWED, key=len, reverse=True)  # longest first so "❤‍🔥" wins over "❤"

MAX_REACTIONS = 20


def parse_reactions(text: str) -> tuple[list[str], list[str]]:
    """Return (valid_emoji, invalid_chars). Accepts '👍❤🔥', '👍, ❤️, 🔥', etc."""
    s = text.replace(VS16, "")
    valid: list[str] = []
    invalid: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch.isspace() or ch in ",;|" or ch == ZWJ:
            i += 1
            continue
        for cand in _BY_LENGTH:
            if s.startswith(cand, i):
                if cand not in valid:
                    valid.append(cand)
                i += len(cand)
                break
        else:
            if ch not in invalid:
                invalid.append(ch)
            i += 1
    return valid, invalid


# Service messages (joins, pins, title changes…) are not reacted to.
SERVICE_KEYS = frozenset({
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "group_chat_created", "supergroup_chat_created",
    "channel_chat_created", "migrate_to_chat_id", "migrate_from_chat_id",
    "pinned_message", "message_auto_delete_timer_changed", "video_chat_started",
    "video_chat_ended", "video_chat_scheduled", "video_chat_participants_invited",
    "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
    "forum_topic_reopened", "boost_added", "chat_background_set",
})


def pick_delay(settings: dict) -> float:
    """Seconds to wait before reacting.

    Human-like mode: a fresh random value in [min, max] for EVERY reaction, so actions are
    spread out instead of firing in a burst. Otherwise the fixed `reaction_delay`.
    """
    rd = settings.get("random_delay") or {}
    if rd.get("enabled"):
        lo, hi = sorted((float(rd.get("min", 2)), float(rd.get("max", 60))))
        return random.uniform(lo, hi)
    return float(settings.get("reaction_delay", 0) or 0)


async def react_to_update(api, update: dict, reactions, delay: float, counters=None) -> bool:
    """React to a message/channel post. Sleeps `delay` seconds first (asyncio.sleep).

    `counters` (a collections.Counter) collects messages / reactions / failed for stats."""
    msg = update.get("message") or update.get("channel_post")
    if not msg or SERVICE_KEYS & msg.keys():
        return False
    if counters is not None:
        counters["messages"] += 1
    if not reactions:
        return False

    emoji = random.choice(list(reactions))
    if delay and delay > 0:
        await asyncio.sleep(delay)

    chat_id, message_id = msg["chat"]["id"], msg["message_id"]
    for attempt in (1, 2):
        try:
            await api.set_message_reaction(chat_id, message_id, emoji)
            if counters is not None:
                counters["reactions"] += 1
            return True
        except TelegramError as e:
            if e.code == 429 and attempt == 1 and (e.retry_after or 0) <= 30:
                await asyncio.sleep((e.retry_after or 1) + 0.5)
                continue
            log.debug("reaction failed in %s: %s", chat_id, e.description)
            if counters is not None:
                counters["failed"] += 1
            return False
    return False
