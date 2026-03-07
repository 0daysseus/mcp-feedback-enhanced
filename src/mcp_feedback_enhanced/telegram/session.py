#!/usr/bin/env python3
"""
Telegram feedback session configuration.
"""

import asyncio
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"


class TelegramFeedbackCancelled(RuntimeError):
    """Raised when a Telegram feedback session is cancelled by the user."""


@dataclass(slots=True)
class TelegramFeedbackSession:
    """Configuration-backed Telegram feedback session."""

    summary: str
    project_directory: str
    bot_token: str
    chat_id: str
    api_base: str = DEFAULT_TELEGRAM_API_BASE
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    start_time: float = field(default_factory=time.time)
    next_update_offset: int | None = None

    @classmethod
    def from_environment(
        cls, summary: str, project_directory: str
    ) -> "TelegramFeedbackSession":
        """Build a Telegram session from environment configuration."""
        bot_token = os.getenv("MCP_TELEGRAM_BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("Missing required environment variable: MCP_TELEGRAM_BOT_TOKEN")

        chat_id = os.getenv("MCP_TELEGRAM_CHAT_ID", "").strip()
        if not chat_id:
            raise ValueError("Missing required environment variable: MCP_TELEGRAM_CHAT_ID")

        api_base = os.getenv("MCP_TELEGRAM_API_BASE", DEFAULT_TELEGRAM_API_BASE).strip()
        if not api_base:
            api_base = DEFAULT_TELEGRAM_API_BASE

        return cls(
            summary=summary,
            project_directory=project_directory,
            bot_token=bot_token,
            chat_id=chat_id,
            api_base=api_base.rstrip("/"),
        )

    async def collect_feedback(
        self, client: Any, timeout: int
    ) -> dict[str, str | list[dict[str, Any]]]:
        """Collect Telegram text and image replies until done, cancel, or timeout."""
        if timeout <= 0:
            raise TimeoutError("Telegram feedback session timed out")

        await self._bootstrap_update_offset(client)

        deadline = time.monotonic() + timeout
        text_fragments: list[str] = []
        images: list[dict[str, Any]] = []

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            poll_timeout = max(0, min(30, int(remaining)))
            updates = await asyncio.wait_for(
                client.get_updates(
                    offset=self.next_update_offset,
                    timeout=poll_timeout,
                ),
                timeout=max(remaining, 0.01),
            )
            for update in updates:
                result = await self._process_update(update, client, text_fragments, images)
                if result == "done":
                    return {
                        "command_logs": "",
                        "interactive_feedback": "\n\n".join(text_fragments),
                        "images": images,
                    }
                if result == "cancel":
                    raise TelegramFeedbackCancelled("Telegram feedback session cancelled")

            if not updates:
                await asyncio.sleep(0.05)

        raise TimeoutError("Telegram feedback session timed out")

    async def _bootstrap_update_offset(self, client: Any) -> None:
        """Prime the update offset so backlog messages are ignored by default."""
        if self.next_update_offset is not None:
            return

        updates = await client.get_updates(timeout=0)
        highest_update_id = 0

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                highest_update_id = max(highest_update_id, update_id)

        self.next_update_offset = highest_update_id + 1

    async def _process_update(
        self,
        update: Mapping[str, Any],
        client: Any,
        text_fragments: list[str],
        images: list[dict[str, Any]],
    ) -> str | None:
        """Process a single Telegram update for this session."""
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            if self.next_update_offset is not None and update_id < self.next_update_offset:
                return None
            self.next_update_offset = update_id + 1

        message = update.get("message")
        if not isinstance(message, Mapping):
            return None

        if not self._is_target_chat(message):
            return None

        text = message.get("text")
        if isinstance(text, str):
            stripped_text = text.strip()
            if stripped_text == "/done":
                return "done"
            if stripped_text == "/cancel":
                return "cancel"
            if stripped_text:
                text_fragments.append(stripped_text)

        caption = message.get("caption")
        if isinstance(caption, str):
            stripped_caption = caption.strip()
            if stripped_caption:
                text_fragments.append(stripped_caption)

        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            largest_photo = self._select_largest_photo(photo)
            if largest_photo is not None:
                image = await self._download_photo(largest_photo, client)
                if image is not None:
                    images.append(image)

        return None

    def _is_target_chat(self, message: Mapping[str, Any]) -> bool:
        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            return False
        return str(chat.get("id")) == self.chat_id

    def _select_largest_photo(self, photo_sizes: list[Any]) -> Mapping[str, Any] | None:
        valid_sizes = [item for item in photo_sizes if isinstance(item, Mapping)]
        if not valid_sizes:
            return None
        return max(
            valid_sizes,
            key=lambda item: int(item.get("file_size", 0) or 0),
        )

    async def _download_photo(
        self, photo: Mapping[str, Any], client: Any
    ) -> dict[str, Any] | None:
        file_id = photo.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            return None

        file_info = await client.get_file(file_id)
        file_path = file_info.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return None

        image_bytes = await client.download_file(file_path)
        return {
            "name": Path(file_path).name,
            "data": image_bytes,
            "size": len(image_bytes),
        }
