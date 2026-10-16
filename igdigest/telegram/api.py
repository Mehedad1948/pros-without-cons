"""Minimal synchronous Telegram Bot API client (https://core.telegram.org/bots/api).

The bot token is part of every request URL, so it is redacted from every error message and
httpx request logging is kept at WARNING by the CLI.
"""

from __future__ import annotations

from typing import Any

import httpx

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096


class TelegramError(Exception):
    def __init__(self, method: str, code: int | None, description: str, retry_after: int | None = None):
        super().__init__(f"Telegram {method} failed ({code}): {description}")
        self.method, self.code, self.description, self.retry_after = method, code, description, retry_after


class TelegramNetworkError(TelegramError):
    pass


class TelegramAPI:
    def __init__(self, token: str, proxy: str | None = None, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        if not token:
            raise ValueError("Telegram bot token is empty (IGD_TELEGRAM_BOT_TOKEN)")
        self._token = token
        self._base = f"{API_BASE}/bot{token}/"
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        elif proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.Client(**kwargs)

    def close(self) -> None:
        self._client.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    def call(self, method: str, http_timeout: float | None = None, **params) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._client.post(self._base + method, json=payload,
                                     timeout=http_timeout if http_timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TransportError as e:
            raise TelegramNetworkError(method, None, f"network error: {type(e).__name__}") from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(method, resp.status_code, "non-JSON response") from None
        if not data.get("ok"):
            params_ = data.get("parameters") or {}
            raise TelegramError(method, data.get("error_code", resp.status_code),
                                self._redact(str(data.get("description", "unknown error"))),
                                params_.get("retry_after"))
        return data.get("result")

    # --- methods used by the bot --------------------------------------------
    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int | None, poll_seconds: int) -> list[dict]:
        """Long polling: Telegram holds the request up to poll_seconds; the HTTP timeout is longer."""
        return self.call(
            "getUpdates", http_timeout=poll_seconds + 15,
            offset=offset, timeout=poll_seconds, allowed_updates=["message", "callback_query"],
        )

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None,
                     preview: bool = False) -> dict:
        return self.call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=reply_markup,
            link_preview_options=None if preview else {"is_disabled": True},
        )

    def send_photo(self, chat_id: int, photo_url: str, caption: str, reply_markup: dict | None = None) -> dict:
        """Telegram downloads the image from the URL itself (photos up to 5 MB)."""
        return self.call("sendPhoto", chat_id=chat_id, photo=photo_url, caption=caption, parse_mode="HTML",
                         reply_markup=reply_markup)

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> Any:
        try:
            return self.call(
                "editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                reply_markup=reply_markup, link_preview_options={"is_disabled": True},
            )
        except TelegramError as e:
            if "message is not modified" in e.description:
                return None
            raise

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> Any:
        return self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def set_my_commands(self, commands: list[tuple[str, str]]) -> Any:
        return self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])
