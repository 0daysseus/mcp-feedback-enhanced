#!/usr/bin/env python3
"""
Shared Telegram feedback-session state for server tools and the gateway.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock
from typing import Any

from .session import TelegramFeedbackCancelled


@dataclass(slots=True)
class PendingTelegramFeedback:
    """One pending Telegram feedback request."""

    chat_id: str
    project_directory: str
    summary: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    message_id: int | None = None
    text_fragments: list[str] = field(default_factory=list, repr=False)
    images: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _resolved: Event = field(default_factory=Event, repr=False)
    _cancelled: bool = field(default=False, repr=False)

    def finish(self) -> None:
        """Resolve the request with the accumulated feedback."""
        self._resolved.set()

    def cancel(self) -> None:
        """Resolve the request as cancelled."""
        self._cancelled = True
        self._resolved.set()

    def wait(self, timeout: float | None = None) -> dict[str, str | list[dict[str, Any]]]:
        """Block until Telegram resolves the request or the wait times out."""
        if self._resolved.wait(timeout):
            if self._cancelled:
                raise TelegramFeedbackCancelled("Telegram feedback session cancelled")
            return {
                "command_logs": "",
                "interactive_feedback": "\n\n".join(self.text_fragments),
                "images": list(self.images),
            }
        raise TimeoutError("Telegram feedback session timed out")

    async def process_message(self, message: Mapping[str, Any], client: Any) -> bool:
        """Consume one Telegram message when it belongs to this pending request."""
        if not _message_matches_chat(message, self.chat_id):
            return False

        text = message.get("text")
        if isinstance(text, str):
            stripped_text = text.strip()
            if stripped_text == "/done":
                self.finish()
                return True
            if stripped_text == "/cancel":
                self.cancel()
                return True
            if stripped_text.startswith("/"):
                return False
            if stripped_text:
                self.text_fragments.append(stripped_text)
                return True

        handled = False

        caption = message.get("caption")
        if isinstance(caption, str):
            stripped_caption = caption.strip()
            if stripped_caption:
                self.text_fragments.append(stripped_caption)
                handled = True

        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            largest_photo = _select_largest_photo(photo)
            if largest_photo is not None:
                image = await _download_photo(largest_photo, client)
                if image is not None:
                    self.images.append(image)
                    handled = True

        return handled


_PENDING_FEEDBACK_REQUESTS: dict[str, PendingTelegramFeedback] = {}
_PENDING_FEEDBACK_LOCK = Lock()


def create_pending_feedback_request(
    chat_id: str,
    project_directory: str,
    summary: str,
) -> PendingTelegramFeedback:
    """Register and return one pending Telegram feedback request."""
    request = PendingTelegramFeedback(
        chat_id=chat_id,
        project_directory=project_directory,
        summary=summary,
    )
    with _PENDING_FEEDBACK_LOCK:
        _PENDING_FEEDBACK_REQUESTS[request.request_id] = request
    return request


def register_pending_feedback_message(request_id: str, message_id: int | None) -> None:
    """Attach the Telegram prompt message id to a pending request when available."""
    if message_id is None:
        return
    with _PENDING_FEEDBACK_LOCK:
        request = _PENDING_FEEDBACK_REQUESTS.get(request_id)
        if request is not None:
            request.message_id = message_id


def discard_pending_feedback_request(request_id: str) -> None:
    """Remove a pending feedback request without resolving it."""
    with _PENDING_FEEDBACK_LOCK:
        _PENDING_FEEDBACK_REQUESTS.pop(request_id, None)


async def wait_for_pending_feedback(
    request: PendingTelegramFeedback,
    timeout: float,
) -> dict[str, str | list[dict[str, Any]]]:
    """Wait asynchronously for Telegram feedback routed through the gateway."""
    try:
        return await asyncio.to_thread(request.wait, timeout)
    finally:
        discard_pending_feedback_request(request.request_id)


async def handle_pending_feedback_message(message: Mapping[str, Any], client: Any) -> bool:
    """Deliver a Telegram message to the newest pending feedback request for that chat."""
    chat_id = _extract_chat_id(message)
    if chat_id is None:
        return False

    request = _latest_pending_feedback_for_chat(chat_id)
    if request is None:
        return False

    handled = await request.process_message(message, client)
    if handled and request._resolved.is_set():
        discard_pending_feedback_request(request.request_id)
    return handled


def _latest_pending_feedback_for_chat(chat_id: str) -> PendingTelegramFeedback | None:
    with _PENDING_FEEDBACK_LOCK:
        matches = [
            request
            for request in _PENDING_FEEDBACK_REQUESTS.values()
            if request.chat_id == chat_id
        ]
    if not matches:
        return None
    return max(matches, key=lambda request: request.created_at)


def _extract_chat_id(message: Mapping[str, Any]) -> str | None:
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return None
    chat_id = chat.get("id")
    if chat_id is None:
        return None
    return str(chat_id)


def _message_matches_chat(message: Mapping[str, Any], chat_id: str) -> bool:
    return _extract_chat_id(message) == chat_id


def _select_largest_photo(photo_sizes: list[Any]) -> Mapping[str, Any] | None:
    valid_sizes = [item for item in photo_sizes if isinstance(item, Mapping)]
    if not valid_sizes:
        return None
    return max(
        valid_sizes,
        key=lambda item: int(item.get("file_size", 0) or 0),
    )


async def _download_photo(
    photo: Mapping[str, Any], client: Any
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
