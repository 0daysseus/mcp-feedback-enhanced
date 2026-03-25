#!/usr/bin/env python3
"""
Telegram feedback support.
"""

from .client import TelegramBotClient, TelegramClientError
from .completion_confirmation import (
    build_completion_confirmation_keyboard,
    create_completion_confirmation_request,
    discard_completion_confirmation_request,
    register_completion_confirmation_message,
    resolve_completion_confirmation,
    wait_for_completion_confirmation,
)
from .pending_feedback import (
    create_pending_feedback_request,
    discard_pending_feedback_request,
    handle_pending_feedback_message,
    register_pending_feedback_message,
    wait_for_pending_feedback,
)
from .session import (
    DEFAULT_TELEGRAM_API_BASE,
    TelegramFeedbackCancelled,
    TelegramFeedbackSession,
)


__all__ = [
    "DEFAULT_TELEGRAM_API_BASE",
    "TelegramBotClient",
    "TelegramClientError",
    "TelegramFeedbackCancelled",
    "TelegramFeedbackSession",
    "build_completion_confirmation_keyboard",
    "create_completion_confirmation_request",
    "create_pending_feedback_request",
    "discard_completion_confirmation_request",
    "discard_pending_feedback_request",
    "handle_pending_feedback_message",
    "register_completion_confirmation_message",
    "register_pending_feedback_message",
    "resolve_completion_confirmation",
    "wait_for_completion_confirmation",
    "wait_for_pending_feedback",
]
