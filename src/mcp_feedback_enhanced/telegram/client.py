#!/usr/bin/env python3
"""
Minimal Telegram Bot API client.
"""

import os
from collections.abc import Callable
from typing import Any

import aiohttp

from .session import DEFAULT_TELEGRAM_API_BASE


class TelegramClientError(RuntimeError):
    """Raised when Telegram Bot API requests fail."""


class TelegramBotClient:
    """Narrow async wrapper over Telegram Bot API endpoints."""

    def __init__(
        self,
        token: str,
        api_base: str = DEFAULT_TELEGRAM_API_BASE,
        session_factory: Callable[[], Any] = aiohttp.ClientSession,
    ):
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._session_factory = session_factory
        self._request_kwargs = self._build_request_kwargs()

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a text message to a Telegram chat."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._post_json("sendMessage", payload)

    async def get_updates(
        self, offset: int | None = None, timeout: int = 30
    ) -> list[dict[str, Any]]:
        """Fetch updates for the bot."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        result = await self._get_json("getUpdates", params=params)
        return result if isinstance(result, list) else []

    async def get_commands(self) -> list[dict[str, str]]:
        """Fetch the bot's global slash command list."""
        result = await self._get_json("getMyCommands")
        return result if isinstance(result, list) else []

    async def set_commands(self, commands: list[dict[str, str]]) -> bool:
        """Set the bot's global slash command list."""
        result = await self._post_json("setMyCommands", {"commands": commands})
        return bool(result)

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        """Edit an existing Telegram message."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._post_json("editMessageText", payload)
        return bool(result)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
    ) -> bool:
        """Acknowledge a Telegram callback query."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        result = await self._post_json("answerCallbackQuery", payload)
        return bool(result)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        """Fetch file metadata for a Telegram file id."""
        result = await self._get_json("getFile", params={"file_id": file_id})
        return result if isinstance(result, dict) else {}

    async def download_file(self, file_path: str) -> bytes:
        """Download a Telegram-hosted file."""
        file_url = f"{self._api_base}/file/bot{self._token}/{file_path.lstrip('/')}"
        async with self._session_factory() as session:
            async with session.get(file_url, **self._request_kwargs) as response:
                if response.status >= 400:
                    raise TelegramClientError(
                        f"Telegram file download failed with status {response.status}"
                    )
                return await response.read()

    async def _post_json(
        self, method_name: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        api_url = self._method_url(method_name)
        async with self._session_factory() as session:
            async with session.post(
                api_url, json=payload, **self._request_kwargs
            ) as response:
                data = await response.json()
                return self._unwrap_result(data, response.status)

    async def _get_json(
        self, method_name: str, params: dict[str, Any] | None = None
    ) -> Any:
        api_url = self._method_url(method_name)
        async with self._session_factory() as session:
            async with session.get(
                api_url, params=params, **self._request_kwargs
            ) as response:
                data = await response.json()
                return self._unwrap_result(data, response.status)

    def _method_url(self, method_name: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method_name}"

    def _build_request_kwargs(self) -> dict[str, Any]:
        if os.getenv("MCP_TELEGRAM_DISABLE_SSL_VERIFY", "").lower() in (
            "true",
            "1",
            "yes",
            "on",
        ):
            return {"ssl": False}
        return {}

    def _unwrap_result(self, data: dict[str, Any], status_code: int) -> Any:
        if status_code >= 400 or not data.get("ok", False):
            description = data.get("description", "Telegram API request failed")
            raise TelegramClientError(str(description))
        return data.get("result")
