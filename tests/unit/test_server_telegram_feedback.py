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

    result = await server.telegram_feedback(".", "summary")

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "MCP_TELEGRAM_BOT_TOKEN" in result[0].text


@pytest.mark.asyncio
async def test_ensure_telegram_commands_keeps_existing_matching_commands():
    client = FakeTelegramCommandClient(
        commands=[
            {"command": "done", "description": "Submit feedback"},
        ]
    )

    await server.ensure_telegram_commands(client)

    assert client.set_commands_calls == []


@pytest.mark.asyncio
async def test_ensure_telegram_commands_registers_missing_required_commands():
    client = FakeTelegramCommandClient(
        commands=[
            {"command": "help", "description": "Show help"},
        ]
    )

    await server.ensure_telegram_commands(client)

    assert client.set_commands_calls == [
        [
            {"command": "help", "description": "Show help"},
            {"command": "done", "description": "Submit feedback"},
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

    assert client.set_commands_calls == []


@pytest.mark.asyncio
async def test_ensure_telegram_commands_drops_obsolete_cancel_command():
    client = FakeTelegramCommandClient(
        commands=[
            {"command": "done", "description": "Submit feedback"},
            {"command": "cancel", "description": "Cancel feedback"},
            {"command": "help", "description": "Show help"},
        ]
    )

    await server.ensure_telegram_commands(client)

    assert client.set_commands_calls == [
        [
            {"command": "done", "description": "Submit feedback"},
            {"command": "help", "description": "Show help"},
        ]
    ]


@pytest.mark.asyncio
async def test_launch_telegram_feedback_does_not_register_commands_during_tool_call(
    monkeypatch,
):
    sent_messages = []

    class FakeSession:
        bot_token = "token"  # noqa: S105 - test stub value
        api_base = "https://api.telegram.org"
        chat_id = "123"

        @classmethod
        def from_environment(cls, summary, project_dir):
            return cls()

        async def collect_feedback(self, client, timeout):
            return {
                "command_logs": "",
                "interactive_feedback": "telegram reply",
                "images": [],
            }

    class FakeClient:
        def __init__(self, token, api_base):
            self.token = token
            self.api_base = api_base

        async def send_message(self, chat_id, text):
            sent_messages.append({"chat_id": chat_id, "text": text})
            return {"message_id": 1}

    async def fail_ensure(client):
        raise AssertionError("tool calls should not register Telegram commands")

    monkeypatch.setattr(server, "TelegramFeedbackSession", FakeSession)
    monkeypatch.setattr(server, "TelegramBotClient", FakeClient)
    monkeypatch.setattr(server, "ensure_telegram_commands", fail_ensure)

    result = await server.launch_telegram_feedback("/tmp/project", "summary", 30)

    assert result["interactive_feedback"] == "telegram reply"
    assert sent_messages == [
        {
            "chat_id": "123",
            "text": (
                "AI 工作摘要:\nsummary\n\n"
                "專案目錄:\n/tmp/project\n\n"
                "請直接回覆文字與圖片。\n"
                "送出請輸入 /done"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_launch_telegram_feedback_uses_background_gateway_when_running(
    monkeypatch,
):
    sent_messages = []
    wait_calls = {}

    class FakeGatewayThread:
        def is_alive(self):
            return True

    class FakeRequest:
        request_id = "feedback-1"

    class FakeSession:
        bot_token = "token"  # noqa: S105 - test stub value
        api_base = "https://api.telegram.org"
        chat_id = "123"

        @classmethod
        def from_environment(cls, summary, project_dir):
            return cls()

        async def collect_feedback(self, client, timeout):
            raise AssertionError(
                "telegram_feedback should not poll getUpdates directly while the background gateway is running"
            )

    class FakeClient:
        def __init__(self, token, api_base):
            self.token = token
            self.api_base = api_base

        async def send_message(self, chat_id, text):
            sent_messages.append({"chat_id": chat_id, "text": text})
            return {"message_id": 17}

    async def fake_wait(request, timeout):
        wait_calls["request_id"] = request.request_id
        wait_calls["timeout"] = timeout
        return {
            "command_logs": "",
            "interactive_feedback": "gateway reply",
            "images": [],
        }

    monkeypatch.setattr(server, "_http_telegram_gateway_thread", FakeGatewayThread())
    monkeypatch.setattr(server, "TelegramFeedbackSession", FakeSession)
    monkeypatch.setattr(server, "TelegramBotClient", FakeClient)
    monkeypatch.setattr(
        server,
        "create_pending_feedback_request",
        lambda chat_id, project_directory, summary: FakeRequest(),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "register_pending_feedback_message",
        lambda request_id, message_id: wait_calls.update(
            {"registered_request_id": request_id, "message_id": message_id}
        ),
        raising=False,
    )
    monkeypatch.setattr(
        server,
        "wait_for_pending_feedback",
        fake_wait,
        raising=False,
    )

    result = await server.launch_telegram_feedback("/tmp/project", "summary", 30)

    assert result == {
        "command_logs": "",
        "interactive_feedback": "gateway reply",
        "images": [],
    }
    assert wait_calls == {
        "registered_request_id": "feedback-1",
        "message_id": 17,
        "request_id": "feedback-1",
        "timeout": 30,
    }
    assert sent_messages == [
        {
            "chat_id": "123",
            "text": (
                "AI 工作摘要:\nsummary\n\n"
                "專案目錄:\n/tmp/project\n\n"
                "請直接回覆文字與圖片。\n"
                "送出請輸入 /done"
            ),
        }
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

    result = await server.telegram_feedback(".", "summary")

    assert len(result) == 2
    assert isinstance(result[0], TextContent)
    assert "telegram reply" in result[0].text
    assert isinstance(result[1], MCPImage)


@pytest.mark.asyncio
async def test_telegram_confirm_completion_returns_structured_decision(monkeypatch):
    sent_messages = []
    registered = {}

    class FakeSession:
        bot_token = "token"  # noqa: S105 - test stub value
        api_base = "https://api.telegram.org"
        chat_id = "123"

        @classmethod
        def from_environment(cls, summary, project_dir):
            return cls()

    class FakeClient:
        def __init__(self, token, api_base):
            self.token = token
            self.api_base = api_base

        async def send_message(self, chat_id, text, reply_markup=None):
            sent_messages.append(
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_markup": reply_markup,
                }
            )
            return {"message_id": 9}

    class FakeRequest:
        request_id = "confirm-1"

    async def fake_wait(request, timeout):
        assert request.request_id == "confirm-1"
        assert timeout == 88
        return {
            "approved": True,
            "decision": "approved",
            "response_text": "Approved from Telegram",
        }

    monkeypatch.setattr(server, "TelegramFeedbackSession", FakeSession)
    monkeypatch.setattr(server, "TelegramBotClient", FakeClient)
    monkeypatch.setattr(server, "_normalize_project_directory", lambda value: value)
    monkeypatch.setattr(server, "_resolve_feedback_timeout", lambda: 88)
    monkeypatch.setattr(
        server,
        "create_completion_confirmation_request",
        lambda chat_id, project_directory, summary: FakeRequest(),
    )
    monkeypatch.setattr(
        server,
        "build_completion_confirmation_keyboard",
        lambda request_id: {
            "inline_keyboard": [
                [
                    {
                        "text": "Approve",
                        "callback_data": f"tcc:approve:{request_id}",
                    }
                ]
            ]
        },
    )
    monkeypatch.setattr(
        server,
        "register_completion_confirmation_message",
        lambda request_id, message_id: registered.update(
            {"request_id": request_id, "message_id": message_id}
        ),
    )
    monkeypatch.setattr(server, "wait_for_completion_confirmation", fake_wait)

    result = await server.telegram_confirm_completion("/tmp/project", "summary")

    assert result == {
        "approved": True,
        "decision": "approved",
        "response_text": "Approved from Telegram",
    }
    assert registered == {"request_id": "confirm-1", "message_id": 9}
    assert sent_messages == [
        {
            "chat_id": "123",
            "text": (
                "Task completion check:\nsummary\n\n"
                "Project directory:\n/tmp/project\n\n"
                "Should the agent stop now?"
            ),
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {
                            "text": "Approve",
                            "callback_data": "tcc:approve:confirm-1",
                        }
                    ]
                ]
            },
        }
    ]


@pytest.mark.asyncio
async def test_telegram_confirm_completion_rejects_timeout_argument():
    with pytest.raises(TypeError):
        await server.telegram_confirm_completion(".", "summary", 30)


@pytest.mark.asyncio
async def test_telegram_feedback_uses_timeout_from_environment(monkeypatch):
    captured: dict[str, int] = {}

    async def fake_launch(project_dir, summary, timeout):
        captured["timeout"] = timeout
        return {
            "command_logs": "",
            "interactive_feedback": "telegram reply",
            "images": [],
        }

    monkeypatch.setattr(server, "launch_telegram_feedback", fake_launch)
    monkeypatch.setenv("MCP_FEEDBACK_TIMEOUT", "88")

    result = await server.telegram_feedback(".", "summary")

    assert len(result) == 1
    assert captured["timeout"] == 88


@pytest.mark.asyncio
async def test_telegram_feedback_rejects_timeout_argument():
    with pytest.raises(TypeError):
        await server.telegram_feedback(".", "summary", 30)


@pytest.mark.asyncio
async def test_telegram_feedback_invalid_env_timeout_falls_back_to_default(
    monkeypatch,
):
    captured: dict[str, int] = {}

    async def fake_launch(project_dir, summary, timeout):
        captured["timeout"] = timeout
        return {
            "command_logs": "",
            "interactive_feedback": "telegram reply",
            "images": [],
        }

    monkeypatch.setattr(server, "launch_telegram_feedback", fake_launch)
    monkeypatch.setenv("MCP_FEEDBACK_TIMEOUT", "bad-timeout")

    await server.telegram_feedback(".", "summary")

    assert captured["timeout"] == 600


@pytest.mark.asyncio
async def test_interactive_feedback_still_returns_text_content(monkeypatch):
    async def fake_launch(project_dir, summary, timeout):
        return {
            "command_logs": "",
            "interactive_feedback": "web reply",
            "images": [],
        }

    monkeypatch.setattr(server, "is_remote_environment", lambda: False)
    monkeypatch.setattr(server, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(server, "launch_web_feedback_ui", fake_launch)
    monkeypatch.setattr(server, "is_user_away", lambda: False)

    result = await server.interactive_feedback(".", "summary")

    assert len(result) == 1
    assert isinstance(result[0], TextContent)
    assert "web reply" in result[0].text


@pytest.mark.asyncio
async def test_interactive_feedback_raises_tool_error_when_vscode_browser_env_missing_in_remote_headless(
    monkeypatch,
):
    async def fake_launch(project_dir, summary, timeout):
        raise AssertionError("launch_web_feedback_ui should not be called")

    monkeypatch.setattr(server, "launch_web_feedback_ui", fake_launch)
    monkeypatch.setattr(server, "is_remote_environment", lambda: True)
    monkeypatch.setattr(server, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(server, "is_user_away", lambda: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("MCP_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.delenv("VSCODE_IPC_HOOK_CLI", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        await server.interactive_feedback(".", "summary")

    message = str(excinfo.value)
    assert "telegram_feedback" in message
    assert "BROWSER" in message
    assert "VSCODE_IPC_HOOK_CLI" in message


@pytest.mark.asyncio
async def test_interactive_feedback_uses_timeout_from_environment(monkeypatch):
    captured: dict[str, int] = {}

    async def fake_launch(project_dir, summary, timeout):
        captured["timeout"] = timeout
        return {
            "command_logs": "",
            "interactive_feedback": "web reply",
            "images": [],
        }

    monkeypatch.setattr(server, "launch_web_feedback_ui", fake_launch)
    monkeypatch.setattr(server, "is_remote_environment", lambda: False)
    monkeypatch.setattr(server, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(server, "is_user_away", lambda: False)
    monkeypatch.setenv("MCP_FEEDBACK_TIMEOUT", "42")

    result = await server.interactive_feedback(".", "summary")

    assert len(result) == 1
    assert captured["timeout"] == 42


@pytest.mark.asyncio
async def test_interactive_feedback_rejects_timeout_argument():
    with pytest.raises(TypeError):
        await server.interactive_feedback(".", "summary", 30)


@pytest.mark.asyncio
async def test_interactive_feedback_invalid_env_timeout_falls_back_to_default(
    monkeypatch,
):
    captured: dict[str, int] = {}

    async def fake_launch(project_dir, summary, timeout):
        captured["timeout"] = timeout
        return {
            "command_logs": "",
            "interactive_feedback": "web reply",
            "images": [],
        }

    monkeypatch.setattr(server, "launch_web_feedback_ui", fake_launch)
    monkeypatch.setattr(server, "is_remote_environment", lambda: False)
    monkeypatch.setattr(server, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(server, "is_user_away", lambda: False)
    monkeypatch.setenv("MCP_FEEDBACK_TIMEOUT", "invalid-timeout")

    await server.interactive_feedback(".", "summary")

    assert captured["timeout"] == 600


@pytest.mark.asyncio
async def test_interactive_feedback_raises_runtime_error_when_user_is_away(
    monkeypatch,
):
    async def fail_web(project_dir, summary, timeout):
        raise AssertionError("interactive_feedback should be blocked when away mode is on")

    monkeypatch.setattr(server, "launch_web_feedback_ui", fail_web)
    monkeypatch.setattr(server, "is_remote_environment", lambda: False)
    monkeypatch.setattr(server, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(server, "is_user_away", lambda: True)

    with pytest.raises(RuntimeError) as excinfo:
        await server.interactive_feedback(".", "summary")

    message = str(excinfo.value)
    assert "telegram_feedback" in message
    assert "away" in message.lower()


@pytest.mark.asyncio
async def test_interactive_feedback_raises_runtime_error_when_web_feedback_times_out(
    monkeypatch,
):
    async def fake_launch(project_dir, summary, timeout):
        raise TimeoutError("Operation timeout")

    monkeypatch.setattr(server, "launch_web_feedback_ui", fake_launch)
    monkeypatch.setattr(server, "is_remote_environment", lambda: False)
    monkeypatch.setattr(server, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(server, "is_user_away", lambda: False)

    with pytest.raises(RuntimeError) as excinfo:
        await server.interactive_feedback(".", "summary")

    message = str(excinfo.value)
    assert "telegram_feedback" in message
    assert "timeout" in message.lower()
