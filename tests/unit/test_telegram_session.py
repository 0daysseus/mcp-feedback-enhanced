#!/usr/bin/env python3
"""
Telegram session configuration tests
"""

from pathlib import Path

import pytest

from mcp_feedback_enhanced.telegram.session import (
    DEFAULT_TELEGRAM_API_BASE,
    TelegramFeedbackCancelled,
    TelegramFeedbackSession,
)
from tests.fixtures.test_data import TestData


class FakeTelegramClient:
    """Minimal Telegram client stub for session tests."""

    def __init__(self, *, update_batches=None, file_map=None, file_bytes=None):
        self.update_batches = list(update_batches or [])
        self.file_map = dict(file_map or {})
        self.file_bytes = dict(file_bytes or {})
        self.get_updates_calls = []
        self.get_file_calls = []
        self.download_file_calls = []

    async def get_updates(self, offset=None, timeout=30):
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        if self.update_batches:
            return self.update_batches.pop(0)
        return []

    async def get_file(self, file_id):
        self.get_file_calls.append(file_id)
        return {"file_id": file_id, "file_path": self.file_map[file_id]}

    async def download_file(self, file_path):
        self.download_file_calls.append(file_path)
        return self.file_bytes[file_path]


def test_from_environment_requires_bot_token(monkeypatch):
    """Telegram sessions should require a bot token."""
    monkeypatch.delenv("MCP_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv(
        "MCP_TELEGRAM_CHAT_ID", TestData.TELEGRAM_TEST_CONFIG["chat_id"]
    )

    with pytest.raises(ValueError, match="MCP_TELEGRAM_BOT_TOKEN"):
        TelegramFeedbackSession.from_environment(
            summary="Test summary", project_directory="/tmp/project"
        )


def test_from_environment_requires_chat_id(monkeypatch):
    """Telegram sessions should require a target chat id."""
    monkeypatch.setenv(
        "MCP_TELEGRAM_BOT_TOKEN", TestData.TELEGRAM_TEST_CONFIG["bot_token"]
    )
    monkeypatch.delenv("MCP_TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(ValueError, match="MCP_TELEGRAM_CHAT_ID"):
        TelegramFeedbackSession.from_environment(
            summary="Test summary", project_directory="/tmp/project"
        )


def test_from_environment_loads_required_configuration(monkeypatch):
    """Telegram sessions should load environment-backed configuration."""
    monkeypatch.setenv(
        "MCP_TELEGRAM_BOT_TOKEN", TestData.TELEGRAM_TEST_CONFIG["bot_token"]
    )
    monkeypatch.setenv(
        "MCP_TELEGRAM_CHAT_ID", TestData.TELEGRAM_TEST_CONFIG["chat_id"]
    )

    session = TelegramFeedbackSession.from_environment(
        summary="Test summary", project_directory="/tmp/project"
    )

    assert session.summary == "Test summary"
    assert session.project_directory == "/tmp/project"
    assert session.bot_token == TestData.TELEGRAM_TEST_CONFIG["bot_token"]
    assert session.chat_id == TestData.TELEGRAM_TEST_CONFIG["chat_id"]
    assert session.api_base == DEFAULT_TELEGRAM_API_BASE
    assert session.session_id
    assert session.start_time > 0


@pytest.mark.asyncio
async def test_collect_feedback_ignores_updates_from_other_chat():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
    )
    client = FakeTelegramClient(
        update_batches=[
            [],
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 999999},
                        "text": "ignore me",
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "/done",
                    },
                },
            ]
        ]
    )

    result = await session.collect_feedback(client, timeout=1)

    assert result["interactive_feedback"] == ""
    assert result["images"] == []


@pytest.mark.asyncio
async def test_collect_feedback_ignores_stale_update_ids():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
        next_update_offset=2,
    )
    client = FakeTelegramClient(
        update_batches=[
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "stale text",
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "/done",
                    },
                },
            ]
        ]
    )

    result = await session.collect_feedback(client, timeout=1)

    assert result["interactive_feedback"] == ""
    assert session.next_update_offset == 3


@pytest.mark.asyncio
async def test_collect_feedback_accumulates_multiple_text_messages():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
    )
    client = FakeTelegramClient(
        update_batches=[
            [],
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "first reply",
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "second reply",
                    },
                },
                {
                    "update_id": 3,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "/done",
                    },
                },
            ]
        ]
    )

    result = await session.collect_feedback(client, timeout=1)

    assert result["interactive_feedback"] == "first reply\n\nsecond reply"
    assert result["images"] == []


@pytest.mark.asyncio
async def test_collect_feedback_appends_caption_and_downloads_largest_photo():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
    )
    client = FakeTelegramClient(
        update_batches=[
            [],
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "photo": [
                            {"file_id": "small-file", "file_size": 10},
                            {"file_id": "large-file", "file_size": 20},
                        ],
                        "caption": "see attached",
                    },
                },
                {
                    "update_id": 2,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "/done",
                    },
                },
            ]
        ],
        file_map={"large-file": "photos/large.jpg"},
        file_bytes={"photos/large.jpg": b"large-image-bytes"},
    )

    result = await session.collect_feedback(client, timeout=1)

    assert result["interactive_feedback"] == "see attached"
    assert client.get_file_calls == ["large-file"]
    assert client.download_file_calls == ["photos/large.jpg"]
    assert result["images"] == [
        {
            "name": Path("photos/large.jpg").name,
            "data": b"large-image-bytes",
            "size": len(b"large-image-bytes"),
        }
    ]


@pytest.mark.asyncio
async def test_collect_feedback_raises_cancelled_on_cancel():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
    )
    client = FakeTelegramClient(
        update_batches=[
            [],
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "/cancel",
                    },
                }
            ]
        ]
    )

    with pytest.raises(TelegramFeedbackCancelled):
        await session.collect_feedback(client, timeout=1)


@pytest.mark.asyncio
async def test_collect_feedback_raises_timeout_without_done():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
    )
    client = FakeTelegramClient(update_batches=[[]])

    with pytest.raises(TimeoutError):
        await session.collect_feedback(client, timeout=0)


@pytest.mark.asyncio
async def test_collect_feedback_bootstraps_offset_to_skip_backlog():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
    )
    client = FakeTelegramClient(
        update_batches=[
            [
                {
                    "update_id": 7,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "stale backlog",
                    },
                }
            ],
            [
                {
                    "update_id": 8,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "fresh reply",
                    },
                },
                {
                    "update_id": 9,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "/done",
                    },
                },
            ],
        ]
    )

    result = await session.collect_feedback(client, timeout=1)

    assert result["interactive_feedback"] == "fresh reply"
    assert session.next_update_offset == 10


@pytest.mark.asyncio
async def test_collect_feedback_limits_poll_timeout_to_session_timeout():
    session = TelegramFeedbackSession(
        summary="Test summary",
        project_directory="/tmp/project",
        bot_token=TestData.TELEGRAM_TEST_CONFIG["bot_token"],
        chat_id=TestData.TELEGRAM_TEST_CONFIG["chat_id"],
    )
    client = FakeTelegramClient(
        update_batches=[
            [],
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])},
                        "text": "/done",
                    },
                }
            ],
        ]
    )

    await session.collect_feedback(client, timeout=1)

    assert client.get_updates_calls
    assert all(call["timeout"] <= 1 for call in client.get_updates_calls)
