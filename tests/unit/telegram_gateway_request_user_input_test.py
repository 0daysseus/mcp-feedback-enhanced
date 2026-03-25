#!/usr/bin/env python3
"""
Telegram gateway request_user_input flow tests.
"""

import asyncio

import pytest

from mcp_feedback_enhanced.telegram import codex_app_server, gateway
from tests.unit.test_telegram_gateway import FakeGatewayClient


def build_question(
    question_id: str,
    prompt: str,
    options: list[str],
) -> codex_app_server.RequestUserInputQuestion:
    return codex_app_server.RequestUserInputQuestion(
        question_id=question_id,
        header=question_id.replace("_", " ").title(),
        question=prompt,
        options=tuple(
            codex_app_server.RequestUserInputOption(
                label=option,
                description=None,
            )
            for option in options
        ),
    )


@pytest.mark.asyncio
async def test_request_user_input_collects_answers_sequentially(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    running_job = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root.resolve(),
        prompt="fix tests",
        requested_action="submit",
        handle=object(),
        process=None,
    )
    service.active_jobs["123"] = running_job
    request = codex_app_server.RequestUserInputRequest(
        request_id=42,
        thread_id="thread-1",
        turn_id="turn-1",
        item_id="item-1",
        questions=(
            build_question(
                "confirm_path",
                "Choose a path",
                ["Use current path (Recommended)", "Pick another path"],
            ),
            build_question(
                "execution_mode",
                "Choose an execution mode",
                ["Safe", "Fast"],
            ),
        ),
    )

    result_task = asyncio.create_task(
        service._handle_request_user_input(running_job, request)
    )
    await asyncio.sleep(0)

    assert client.send_calls
    assert "Choose a path" in client.send_calls[-1]["text"]
    assert running_job.pending_user_input is not None
    assert running_job.latest_status == "Waiting for user input"

    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "rui:pick:0",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert client.edit_calls
    assert "Choose an execution mode" in client.edit_calls[-1]["text"]
    assert running_job.pending_user_input is not None

    await service.handle_callback_query(
        {
            "id": "cb-2",
            "data": "rui:pick:1",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    result = await result_task

    assert result == {
        "answers": {
            "confirm_path": {
                "answers": ["Use current path (Recommended)"],
            },
            "execution_mode": {
                "answers": ["Fast"],
            },
        }
    }
    assert running_job.pending_user_input is None
    assert running_job.latest_status == "Submitted user input"
    assert "Codex is resuming" in client.edit_calls[-1]["text"]
