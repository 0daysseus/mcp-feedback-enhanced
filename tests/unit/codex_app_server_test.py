#!/usr/bin/env python3
"""
Codex app-server request handling tests.
"""

import pytest

from mcp_feedback_enhanced.telegram import codex_app_server


@pytest.mark.asyncio
async def test_request_replies_to_request_user_input_with_same_request_id(monkeypatch):
    server = codex_app_server.CodexAppServer(command=["codex"])
    writes: list[dict[str, object]] = []
    messages = iter(
        [
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "item-1",
                    "questions": [
                        {
                            "id": "confirm_path",
                            "header": "Path",
                            "question": "Choose a path",
                            "options": [
                                {
                                    "label": "Use current path (Recommended)",
                                    "description": "Keep the current directory",
                                }
                            ],
                        }
                    ],
                },
            },
            {"jsonrpc": "2.0", "id": 1, "result": {"data": []}},
        ]
    )

    async def fake_start() -> None:
        return None

    async def fake_write(payload: dict[str, object]) -> None:
        writes.append(payload)

    async def fake_read() -> dict[str, object]:
        return next(messages)

    monkeypatch.setattr(server, "start", fake_start)
    monkeypatch.setattr(server, "_write_message", fake_write)
    monkeypatch.setattr(server, "_read_message", fake_read)

    async def handle_request(
        request: codex_app_server.RequestUserInputRequest,
    ) -> dict[str, object]:
        assert request.request_id == 42
        assert request.thread_id == "thread-1"
        assert request.turn_id == "turn-1"
        assert request.item_id == "item-1"
        assert request.questions[0].question_id == "confirm_path"
        assert request.questions[0].options[0].label == "Use current path (Recommended)"
        return {
            "answers": {
                "confirm_path": {
                    "answers": ["Use current path (Recommended)"],
                }
            }
        }

    result = await server.request(
        "thread/list",
        {"limit": 1},
        on_request_user_input=handle_request,
    )

    assert result == {"data": []}
    assert writes == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "thread/list",
            "params": {"limit": 1},
        },
        {
            "jsonrpc": "2.0",
            "id": 42,
            "result": {
                "answers": {
                    "confirm_path": {
                        "answers": ["Use current path (Recommended)"],
                    }
                }
            },
        },
    ]


@pytest.mark.asyncio
async def test_steer_turn_sends_turn_steer_request_and_updates_active_turn(monkeypatch):
    server = codex_app_server.CodexAppServer(command=["codex"])
    server._active_thread_id = "thread-1"
    server._active_turn_id = "turn-1"
    writes: list[dict[str, object]] = []

    async def fake_start() -> None:
        return None

    async def fake_write(payload: dict[str, object]) -> None:
        writes.append(payload)
        assert server._resolve_runtime_response(
            {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"turnId": "turn-1"},
            }
        )

    monkeypatch.setattr(server, "start", fake_start)
    monkeypatch.setattr(server, "_write_message", fake_write)

    turn_id = await server.steer_turn("Actually focus on failing tests first.")

    assert turn_id == "turn-1"
    assert writes == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn/steer",
            "params": {
                "threadId": "thread-1",
                "input": [
                    {
                        "type": "text",
                        "text": "Actually focus on failing tests first.",
                    }
                ],
                "expectedTurnId": "turn-1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_read_thread_sends_thread_read_request(monkeypatch):
    server = codex_app_server.CodexAppServer(command=["codex"])
    writes: list[dict[str, object]] = []

    async def fake_start() -> None:
        return None

    async def fake_write(payload: dict[str, object]) -> None:
        writes.append(payload)

    messages = iter(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "thread": {
                        "id": "thread-1",
                        "status": {"type": "loaded"},
                        "turns": [],
                    }
                },
            }
        ]
    )

    async def fake_read() -> dict[str, object]:
        return next(messages)

    monkeypatch.setattr(server, "start", fake_start)
    monkeypatch.setattr(server, "_write_message", fake_write)
    monkeypatch.setattr(server, "_read_message", fake_read)

    result = await server.read_thread("thread-1", include_turns=True)

    assert result == {
        "thread": {
            "id": "thread-1",
            "status": {"type": "loaded"},
            "turns": [],
        }
    }
    assert writes == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "thread/read",
            "params": {
                "threadId": "thread-1",
                "includeTurns": True,
            },
        }
    ]
