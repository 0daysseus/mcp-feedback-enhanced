#!/usr/bin/env python3
"""
Telegram feedback integration tests with mocked Bot API behavior.
"""

import pytest
from fastmcp.utilities.types import Image as MCPImage
from mcp.types import TextContent

from mcp_feedback_enhanced import server
from mcp_feedback_enhanced.telegram.client import (
    TelegramBotClient as RealTelegramBotClient,
)
from tests.fixtures.test_data import TestData


class FakeHTTPResponse:
    """Minimal async HTTP response stub."""

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


class FakeHTTPSession:
    """Stateful HTTP session stub shared across client instances."""

    def __init__(self, state):
        self._state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url, json, ssl=None):
        self._state["post_calls"].append({"url": url, "json": json, "ssl": ssl})
        return self._state["post_responses"].pop(0)

    def get(self, url, params=None, ssl=None):
        self._state["get_calls"].append({"url": url, "params": params, "ssl": ssl})
        return self._state["get_responses"].pop(0)


def build_fake_client_class(state):
    """Create a TelegramBotClient variant that uses the fake HTTP session."""

    class FakeTelegramBotClient(RealTelegramBotClient):
        def __init__(self, token, api_base):
            super().__init__(
                token=token,
                api_base=api_base,
                session_factory=lambda: FakeHTTPSession(state),
            )

    return FakeTelegramBotClient


@pytest.mark.asyncio
async def test_telegram_feedback_end_to_end_with_mocked_bot_api(monkeypatch):
    monkeypatch.setenv(
        "MCP_TELEGRAM_BOT_TOKEN", TestData.TELEGRAM_TEST_CONFIG["bot_token"]
    )
    monkeypatch.setenv(
        "MCP_TELEGRAM_CHAT_ID", TestData.TELEGRAM_TEST_CONFIG["chat_id"]
    )

    state = {
        "post_calls": [],
        "get_calls": [],
        "post_responses": [
            FakeHTTPResponse(json_body={"ok": True, "result": True}),
            FakeHTTPResponse(json_body={"ok": True, "result": {"message_id": 1}})
        ],
        "get_responses": [
            FakeHTTPResponse(json_body={"ok": True, "result": []}),
            FakeHTTPResponse(json_body={"ok": True, "result": []}),
            FakeHTTPResponse(
                json_body={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {
                                    "id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])
                                },
                                "text": "first reply",
                            },
                        },
                        {
                            "update_id": 2,
                            "message": {
                                "chat": {
                                    "id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])
                                },
                                "photo": [
                                    {"file_id": "small-file", "file_size": 10},
                                    {"file_id": "large-file", "file_size": 20},
                                ],
                                "caption": "see attached",
                            },
                        },
                        {
                            "update_id": 3,
                            "message": {
                                "chat": {
                                    "id": int(TestData.TELEGRAM_TEST_CONFIG["chat_id"])
                                },
                                "text": "/done",
                            },
                        },
                    ],
                }
            ),
            FakeHTTPResponse(
                json_body={
                    "ok": True,
                    "result": {
                        "file_id": "large-file",
                        "file_path": "photos/large.jpg",
                    },
                }
            ),
            FakeHTTPResponse(body=b"large-image-bytes"),
        ],
    }

    monkeypatch.setattr(
        server,
        "TelegramBotClient",
        build_fake_client_class(state),
    )

    result = await server.telegram_feedback("/tmp/project", "summary text", 30)

    assert len(result) == 2
    assert isinstance(result[0], TextContent)
    assert "first reply" in result[0].text
    assert "see attached" in result[0].text
    assert isinstance(result[1], MCPImage)
    assert state["post_calls"]
    assert state["post_calls"][0]["url"].endswith("/setMyCommands")
    assert state["post_calls"][0]["json"] == {
        "commands": [
            {"command": "done", "description": "Submit feedback"},
            {"command": "cancel", "description": "Cancel feedback"},
        ]
    }
    assert state["post_calls"][1]["json"]["chat_id"] == TestData.TELEGRAM_TEST_CONFIG["chat_id"]
    assert "summary text" in state["post_calls"][1]["json"]["text"]
    assert "/done" in state["post_calls"][1]["json"]["text"]


@pytest.mark.asyncio
async def test_telegram_feedback_returns_readable_error_when_send_fails(monkeypatch):
    monkeypatch.setenv(
        "MCP_TELEGRAM_BOT_TOKEN", TestData.TELEGRAM_TEST_CONFIG["bot_token"]
    )
    monkeypatch.setenv(
        "MCP_TELEGRAM_CHAT_ID", TestData.TELEGRAM_TEST_CONFIG["chat_id"]
    )

    state = {
        "post_calls": [],
        "get_calls": [],
        "post_responses": [
            FakeHTTPResponse(
                status=500,
                json_body={"ok": False, "description": "send failed"},
            )
        ],
        "get_responses": [
            FakeHTTPResponse(
                json_body={
                    "ok": True,
                    "result": [
                        {"command": "done", "description": "Submit feedback"},
                        {"command": "cancel", "description": "Cancel feedback"},
                    ],
                }
            )
        ],
    }

    monkeypatch.setattr(
        server,
        "TelegramBotClient",
        build_fake_client_class(state),
    )

    result = await server.telegram_feedback("/tmp/project", "summary text", 30)

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "send failed" in result[0].text


@pytest.mark.asyncio
async def test_telegram_feedback_skips_command_registration_when_commands_match(
    monkeypatch,
):
    monkeypatch.setenv(
        "MCP_TELEGRAM_BOT_TOKEN", TestData.TELEGRAM_TEST_CONFIG["bot_token"]
    )
    monkeypatch.setenv(
        "MCP_TELEGRAM_CHAT_ID", TestData.TELEGRAM_TEST_CONFIG["chat_id"]
    )

    state = {
        "post_calls": [],
        "get_calls": [],
        "post_responses": [
            FakeHTTPResponse(
                status=500,
                json_body={"ok": False, "description": "send failed"},
            )
        ],
        "get_responses": [
            FakeHTTPResponse(
                json_body={
                    "ok": True,
                    "result": [
                        {"command": "done", "description": "Submit feedback"},
                        {"command": "cancel", "description": "Cancel feedback"},
                    ],
                }
            )
        ],
    }

    monkeypatch.setattr(
        server,
        "TelegramBotClient",
        build_fake_client_class(state),
    )

    result = await server.telegram_feedback("/tmp/project", "summary text", 30)

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "send failed" in result[0].text
    assert len(state["post_calls"]) == 1
    assert state["post_calls"][0]["url"].endswith("/sendMessage")
