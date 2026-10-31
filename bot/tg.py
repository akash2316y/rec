"""Minimal async Telegram Bot API client (httpx). Never logs URLs — they contain the token."""
from __future__ import annotations

import httpx


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int | None = None):
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


def make_http_client() -> httpx.AsyncClient:
    # Every bot holds one long-poll connection open, so don't cap total connections.
    return httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=10.0),
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=20),
    )


class TelegramAPI:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._client = client

    async def call(self, method: str, params: dict | None = None, timeout: float = 15.0):
        try:
            resp = await self._client.post(self._base + method, json=params or {}, timeout=timeout)
        except httpx.HTTPError as e:  # type name only: str(e) may contain the URL
            raise TelegramError(method, 0, f"network error ({type(e).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(method, resp.status_code, "invalid response from Telegram") from None
        if not data.get("ok"):
            raise TelegramError(
                method,
                data.get("error_code", resp.status_code),
                data.get("description", "unknown error"),
                (data.get("parameters") or {}).get("retry_after"),
            )
        return data["result"]

    # ── Methods ──
    async def get_me(self):
        return await self.call("getMe")

    async def delete_webhook(self, drop_pending: bool = False):
        return await self.call("deleteWebhook", {"drop_pending_updates": drop_pending})

    async def get_updates(self, offset: int, timeout: int, allowed: list[str]):
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": allowed},
            timeout=timeout + 15,
        )

    async def send_message(self, chat_id, text, keyboard=None, reply_to=None):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if keyboard:
            p["reply_markup"] = {"inline_keyboard": keyboard}
        if reply_to:
            p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return await self.call("sendMessage", p)

    async def edit_message_text(self, chat_id, message_id, text, keyboard=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
             "link_preview_options": {"is_disabled": True}}
        if keyboard:
            p["reply_markup"] = {"inline_keyboard": keyboard}
        return await self.call("editMessageText", p)

    async def delete_message(self, chat_id, message_id):
        return await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def copy_message(self, chat_id, from_chat_id, message_id):
        return await self.call("copyMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id})

    async def answer_callback(self, callback_id, text="", alert=False):
        return await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": alert})

    async def set_message_reaction(self, chat_id, message_id, emoji):
        return await self.call("setMessageReaction", {
            "chat_id": chat_id, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}], "is_big": True,
        })
