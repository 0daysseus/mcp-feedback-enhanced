#!/usr/bin/env python3
"""
Telegram Bot API client tests.
"""

import pytest

from mcp_feedback_enhanced.telegram.client import (
    DEFAULT_TELEGRAM_API_BASE,
    TelegramBotClient,
    TelegramClientError,
)
from tests.fixtures.test_data import TestData


TEST_BOT_TOKEN = TestData.TELEGRAM_TEST_CONFIG["bot_token"]


class FakeResponse:
    """Minimal async response stub."""

    def __init__(self, *, status=200, json_body=None, body=b""):
        self.status = status
        self._json_body = json_body or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json_body

    async def read(self):
        return self._body


class FakeSession:
    """Minimal async session stub."""

    def __init__(self, *, post_responses=None, get_responses=None):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json, ssl=None):
        self.post_calls.append({"url": url, "json": json, "ssl": ssl})
        return self.post_responses.pop(0)

    def get(self, url, params=None, ssl=None):
        self.get_calls.append({"url": url, "params": params, "ssl": ssl})
        return self.get_responses.pop(0)


@pytest.mark.asyncio
async def test_send_message_posts_to_expected_endpoint():
    fake_session = FakeSession(
        post_responses=[
            FakeResponse(json_body={"ok": True, "result": {"message_id": 10}})
        ]
    )
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    result = await client.send_message(chat_id="123456789", text="hello")

    assert result == {"message_id": 10}
    assert fake_session.post_calls == [
        {
            "url": f"{DEFAULT_TELEGRAM_API_BASE}/bot{TEST_BOT_TOKEN}/sendMessage",
            "json": {"chat_id": "123456789", "text": "hello"},
            "ssl": None,
        }
    ]


@pytest.mark.asyncio
async def test_get_updates_passes_offset_and_timeout():
    fake_session = FakeSession(
        get_responses=[FakeResponse(json_body={"ok": True, "result": [{"update_id": 1}]})]
    )
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    result = await client.get_updates(offset=7, timeout=15)

    assert result == [{"update_id": 1}]
    assert fake_session.get_calls == [
        {
            "url": f"{DEFAULT_TELEGRAM_API_BASE}/bot{TEST_BOT_TOKEN}/getUpdates",
            "params": {"offset": 7, "timeout": 15},
            "ssl": None,
        }
    ]


@pytest.mark.asyncio
async def test_get_commands_reads_global_bot_commands():
    fake_session = FakeSession(
        get_responses=[
            FakeResponse(
                json_body={
                    "ok": True,
                    "result": [
                        {"command": "done", "description": "Submit feedback"},
                        {"command": "cancel", "description": "Cancel feedback"},
                    ],
                }
            )
        ]
    )
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    result = await client.get_commands()

    assert result == [
        {"command": "done", "description": "Submit feedback"},
        {"command": "cancel", "description": "Cancel feedback"},
    ]
    assert fake_session.get_calls == [
        {
            "url": f"{DEFAULT_TELEGRAM_API_BASE}/bot{TEST_BOT_TOKEN}/getMyCommands",
            "params": None,
            "ssl": None,
        }
    ]


@pytest.mark.asyncio
async def test_set_commands_posts_global_bot_commands():
    fake_session = FakeSession(
        post_responses=[FakeResponse(json_body={"ok": True, "result": True})]
    )
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    result = await client.set_commands(
        [
            {"command": "done", "description": "Submit feedback"},
            {"command": "cancel", "description": "Cancel feedback"},
        ]
    )

    assert result is True
    assert fake_session.post_calls == [
        {
            "url": f"{DEFAULT_TELEGRAM_API_BASE}/bot{TEST_BOT_TOKEN}/setMyCommands",
            "json": {
                "commands": [
                    {"command": "done", "description": "Submit feedback"},
                    {"command": "cancel", "description": "Cancel feedback"},
                ]
            },
            "ssl": None,
        }
    ]


@pytest.mark.asyncio
async def test_get_file_returns_file_metadata():
    fake_session = FakeSession(
        get_responses=[
            FakeResponse(
                json_body={
                    "ok": True,
                    "result": {"file_id": "abc", "file_path": "photos/file.jpg"},
                }
            )
        ]
    )
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    result = await client.get_file("abc")

    assert result == {"file_id": "abc", "file_path": "photos/file.jpg"}
    assert fake_session.get_calls == [
        {
            "url": f"{DEFAULT_TELEGRAM_API_BASE}/bot{TEST_BOT_TOKEN}/getFile",
            "params": {"file_id": "abc"},
            "ssl": None,
        }
    ]


@pytest.mark.asyncio
async def test_download_file_fetches_bytes():
    fake_session = FakeSession(get_responses=[FakeResponse(body=b"image-bytes")])
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    result = await client.download_file("photos/file.jpg")

    assert result == b"image-bytes"
    assert fake_session.get_calls == [
        {
            "url": f"{DEFAULT_TELEGRAM_API_BASE}/file/bot{TEST_BOT_TOKEN}/photos/file.jpg",
            "params": None,
            "ssl": None,
        }
    ]


@pytest.mark.asyncio
async def test_send_message_raises_project_friendly_error_on_api_failure():
    fake_session = FakeSession(
        post_responses=[
            FakeResponse(
                status=400,
                json_body={"ok": False, "description": "chat not found"},
            )
        ]
    )
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    with pytest.raises(TelegramClientError, match="chat not found"):
        await client.send_message(chat_id="123456789", text="hello")


@pytest.mark.asyncio
async def test_send_message_disables_ssl_verification_when_env_enabled(monkeypatch):
    monkeypatch.setenv("MCP_TELEGRAM_DISABLE_SSL_VERIFY", "true")
    fake_session = FakeSession(
        post_responses=[
            FakeResponse(json_body={"ok": True, "result": {"message_id": 10}})
        ]
    )
    client = TelegramBotClient(token=TEST_BOT_TOKEN, session_factory=lambda: fake_session)

    await client.send_message(chat_id="123456789", text="hello")

    assert fake_session.post_calls == [
        {
            "url": f"{DEFAULT_TELEGRAM_API_BASE}/bot{TEST_BOT_TOKEN}/sendMessage",
            "json": {"chat_id": "123456789", "text": "hello"},
            "ssl": False,
        }
    ]
    monkeypatch.delenv("MCP_TELEGRAM_DISABLE_SSL_VERIFY", raising=False)
