"""Small shared UI helpers (keyboards / message editing)."""
from __future__ import annotations

from .tg import TelegramError

PAGE_SIZE = 8
STATUS_ICON = {"active": "🟢", "paused": "⏸", "revoked": "⚠️"}
STATUS_TEXT = {"active": "🟢 Active", "paused": "⏸ Paused", "revoked": "⚠️ Token revoked"}


def btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def url_btn(text: str, url: str) -> dict:
    return {"text": text, "url": url}


def fmt_delay(seconds) -> str:
    return "instant" if not seconds else f"{seconds:g}s"


def fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC") if hasattr(dt, "strftime") else "—"


async def show(api, chat_id: int, message_id: int, text: str, keyboard: list) -> None:
    """Edit a message, ignoring Telegram's harmless 'message is not modified'."""
    try:
        await api.edit_message_text(chat_id, message_id, text, keyboard)
    except TelegramError as e:
        if "not modified" not in e.description.lower():
            raise


def pager(prefix: str, page: int, total: int, suffix: str = "") -> list[dict]:
    row = []
    if page > 0:
        row.append(btn("⬅️ Prev", f"{prefix}:{page - 1}{suffix}"))
    if (page + 1) * PAGE_SIZE < total:
        row.append(btn("Next ➡️", f"{prefix}:{page + 1}{suffix}"))
    return row
