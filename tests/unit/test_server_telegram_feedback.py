#!/usr/bin/env python3
"""
Server-level Telegram feedback tests.
"""

import pytest
from fastmcp.utilities.types import Image as MCPImage
from mcp.types import TextContent

from mcp_feedback_enhanced import server


class FakeTelegramCommandClient:
    """Minimal client stub for Telegram command registration tests."""

    def __init__(self, commands=None):
        self.commands = list(commands or [])
        self.set_commands_calls = []

    async def get_commands(self):
        return list(self.commands)

    async def set_commands(self, commands):
        self.set_commands_calls.append(commands)
        self.commands = list(commands)
        return True


@pytest.mark.asyncio
async def test_telegram_feedback_returns_readable_error_for_missing_config(
    monkeypatch,
):
    monkeypatch.delenv("MCP_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MCP_TELEGRAM_CHAT_ID", raising=False)

    result = await server.telegram_feedback(".", "summary", 30)

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "MCP_TELEGRAM_BOT_TOKEN" in result[0].text


@pytest.mark.asyncio
async def test_ensure_telegram_commands_keeps_existing_matching_commands():
    client = FakeTelegramCommandClient(
        commands=[
            {"command": "done", "description": "Submit feedback"},
            {"command": "cancel", "description": "Cancel feedback"},
        ]
    )

    await server.ensure_telegram_commands(client)

    assert client.set_commands_calls == []


@pytest.mark.asyncio
async def test_ensure_telegram_commands_registers_missing_required_commands():
    client = FakeTelegramCommandClient(
        commands=[
            {"command": "done", "description": "Submit feedback"},
        ]
    )

    await server.ensure_telegram_commands(client)

    assert client.set_commands_calls == [
        [
            {"command": "done", "description": "Submit feedback"},
            {"command": "cancel", "description": "Cancel feedback"},
        ]
    ]


@pytest.mark.asyncio
async def test_ensure_telegram_commands_preserves_unrelated_existing_commands():
    client = FakeTelegramCommandClient(
        commands=[
            {"command": "done", "description": "Submit feedback"},
            {"command": "help", "description": "Show help"},
        ]
    )

    await server.ensure_telegram_commands(client)

    assert client.set_commands_calls == [
        [
            {"command": "done", "description": "Submit feedback"},
            {"command": "help", "description": "Show help"},
            {"command": "cancel", "description": "Cancel feedback"},
        ]
    ]


@pytest.mark.asyncio
async def test_telegram_feedback_returns_text_content_and_images(monkeypatch):
    async def fake_launch(project_dir, summary, timeout):
        return {
            "command_logs": "",
            "interactive_feedback": "telegram reply",
            "images": [
                {
                    "name": "photo.png",
                    "data": b"png-bytes",
                    "size": len(b"png-bytes"),
                }
            ],
        }

    monkeypatch.setattr(server, "launch_telegram_feedback", fake_launch)

    result = await server.telegram_feedback(".", "summary", 30)

    assert len(result) == 2
    assert isinstance(result[0], TextContent)
    assert "telegram reply" in result[0].text
    assert isinstance(result[1], MCPImage)


@pytest.mark.asyncio
async def test_interactive_feedback_still_returns_text_content(monkeypatch):
    async def fake_launch(project_dir, summary, timeout):
        return {
            "command_logs": "",
            "interactive_feedback": "web reply",
            "images": [],
        }

    monkeypatch.setattr(server, "launch_web_feedback_ui", fake_launch)

    result = await server.interactive_feedback(".", "summary", 30)

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "web reply" in result[0].text
