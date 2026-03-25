#!/usr/bin/env python3
"""
Shared feedback routing state.

This module keeps a tiny persisted state that both the MCP server and the
Telegram gateway can read so they agree on whether the user is away from the
computer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


FEEDBACK_ROUTE_STATE_FILE_ENV_VAR = "MCP_FEEDBACK_ROUTE_STATE_FILE"
DEFAULT_FEEDBACK_ROUTE_STATE_FILE = (
    Path.home() / ".cache" / "mcp-feedback-enhanced" / "feedback-route-state.json"
)


@dataclass(slots=True)
class FeedbackRouteState:
    """Persisted routing switches shared across runtime entrypoints."""

    user_away: bool = False


def get_feedback_route_state_file() -> Path:
    """Return the state file used for routing preferences."""
    raw_path = os.getenv(FEEDBACK_ROUTE_STATE_FILE_ENV_VAR, "").strip()
    if raw_path:
        return Path(raw_path).expanduser()
    return DEFAULT_FEEDBACK_ROUTE_STATE_FILE


def load_feedback_route_state() -> FeedbackRouteState:
    """Load persisted feedback routing state, defaulting to user-present mode."""
    state_file = get_feedback_route_state_file()
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return FeedbackRouteState()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return FeedbackRouteState()

    if not isinstance(payload, dict):
        return FeedbackRouteState()

    return FeedbackRouteState(user_away=bool(payload.get("user_away", False)))


def save_feedback_route_state(state: FeedbackRouteState) -> None:
    """Persist the shared feedback routing state."""
    state_file = get_feedback_route_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"user_away": state.user_away}, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def is_user_away() -> bool:
    """Return whether feedback should prefer Telegram because the user is away."""
    return load_feedback_route_state().user_away


def set_user_away(enabled: bool) -> FeedbackRouteState:
    """Persist the user away flag and return the new state."""
    state = FeedbackRouteState(user_away=enabled)
    save_feedback_route_state(state)
    return state
