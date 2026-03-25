#!/usr/bin/env python3
"""
Shared Telegram completion-confirmation state for server tools and the gateway.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from threading import Event, Lock


@dataclass(slots=True)
class PendingCompletionConfirmation:
    """One pending Telegram completion-confirmation request."""

    chat_id: str
    project_directory: str
    summary: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)
    message_id: int | None = None
    _resolved: Event = field(default_factory=Event, repr=False)
    _decision: dict[str, object] | None = field(default=None, repr=False)

    def set_decision(self, approved: bool, decision: str, response_text: str) -> None:
        """Resolve the request with one structured Telegram decision."""
        self._decision = {
            "approved": approved,
            "decision": decision,
            "response_text": response_text,
        }
        self._resolved.set()

    def wait(self, timeout: float | None = None) -> dict[str, object]:
        """Block until Telegram resolves the confirmation or the wait times out."""
        if self._resolved.wait(timeout):
            if self._decision is not None:
                return dict(self._decision)

        return {
            "approved": False,
            "decision": "timeout",
            "response_text": "No Telegram response was received before the timeout.",
        }


_PENDING_CONFIRMATIONS: dict[str, PendingCompletionConfirmation] = {}
_PENDING_CONFIRMATIONS_LOCK = Lock()


def create_completion_confirmation_request(
    chat_id: str,
    project_directory: str,
    summary: str,
) -> PendingCompletionConfirmation:
    """Register and return a new pending Telegram completion-confirmation request."""
    request = PendingCompletionConfirmation(
        chat_id=chat_id,
        project_directory=project_directory,
        summary=summary,
    )
    with _PENDING_CONFIRMATIONS_LOCK:
        _PENDING_CONFIRMATIONS[request.request_id] = request
    return request


def register_completion_confirmation_message(request_id: str, message_id: int | None) -> None:
    """Attach the Telegram message id to a pending request when available."""
    if message_id is None:
        return
    with _PENDING_CONFIRMATIONS_LOCK:
        request = _PENDING_CONFIRMATIONS.get(request_id)
        if request is not None:
            request.message_id = message_id


def resolve_completion_confirmation(
    request_id: str,
    *,
    approved: bool,
) -> PendingCompletionConfirmation | None:
    """Resolve and remove one pending completion-confirmation request."""
    with _PENDING_CONFIRMATIONS_LOCK:
        request = _PENDING_CONFIRMATIONS.pop(request_id, None)

    if request is None:
        return None

    if approved:
        request.set_decision(
            True,
            "approved",
            "The Telegram user approved task completion.",
        )
    else:
        request.set_decision(
            False,
            "rejected",
            "The Telegram user rejected task completion and requested more work.",
        )
    return request


def discard_completion_confirmation_request(request_id: str) -> None:
    """Remove a pending request without resolving it."""
    with _PENDING_CONFIRMATIONS_LOCK:
        _PENDING_CONFIRMATIONS.pop(request_id, None)


async def wait_for_completion_confirmation(
    request: PendingCompletionConfirmation,
    timeout: float,
) -> dict[str, object]:
    """Wait asynchronously for Telegram completion confirmation."""
    try:
        return await asyncio.to_thread(request.wait, timeout)
    finally:
        discard_completion_confirmation_request(request.request_id)


def build_completion_confirmation_keyboard(
    request_id: str,
) -> dict[str, list[list[dict[str, str]]]]:
    """Build inline Telegram buttons for completion confirmation."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Approve completion",
                    "callback_data": f"tcc:approve:{request_id}",
                }
            ],
            [
                {
                    "text": "Needs more work",
                    "callback_data": f"tcc:reject:{request_id}",
                }
            ],
        ]
    }
