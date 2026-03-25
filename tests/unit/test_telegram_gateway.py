#!/usr/bin/env python3
"""
Telegram gateway state and Codex command tests.
"""

from pathlib import Path

import pytest

from mcp_feedback_enhanced.telegram import (
    codex_app_server,
    completion_confirmation,
    gateway,
)
from mcp_feedback_enhanced.telegram.client import TelegramClientError


class FakeGatewayClient:
    """Minimal Telegram client stub for gateway tests."""

    def __init__(self):
        self.send_calls = []
        self.edit_calls = []
        self.answer_calls = []
        self._message_id = 100

    async def send_message(self, chat_id, text, reply_markup=None):
        self._message_id += 1
        self.send_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"message_id": self._message_id}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edit_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return True

    async def answer_callback_query(self, callback_query_id, text=None):
        self.answer_calls.append(
            {"callback_query_id": callback_query_id, "text": text}
        )
        return True

    async def get_commands(self):
        return []

    async def set_commands(self, commands):
        self.set_commands_calls = getattr(self, "set_commands_calls", [])
        self.set_commands_calls.append(commands)
        return True


def build_thread_summary(
    directory: Path,
    *,
    thread_id: str = "thread-1",
    display_name: str = "Fix failing tests",
    updated_at: int = 10,
    status_type: str = "notLoaded",
) -> codex_app_server.CodexThreadSummary:
    return codex_app_server.CodexThreadSummary(
        thread_id=thread_id,
        cwd=directory.resolve(),
        preview=display_name,
        name=None,
        updated_at=updated_at,
        status_type=status_type,
    )


def test_resolve_directory_keeps_navigation_within_root(tmp_path):
    root = tmp_path / "home" / "kube"
    nested = root / "projects" / "demo"
    nested.mkdir(parents=True)

    resolved = gateway.resolve_directory(root, nested)

    assert resolved == nested.resolve()


def test_resolve_directory_rejects_escape_outside_root(tmp_path):
    root = tmp_path / "home" / "kube"
    outside = tmp_path / "etc"
    root.mkdir(parents=True)
    outside.mkdir(parents=True)

    with pytest.raises(ValueError, match="outside the allowed root"):
        gateway.resolve_directory(root, root / ".." / ".." / "etc")


def test_list_directory_page_only_returns_directories(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "b-dir").mkdir(parents=True)
    (root / "a-dir").mkdir()
    (root / "notes.txt").write_text("ignore me")

    entries, total_pages = gateway.list_directory_page(root, root, page=0, page_size=10)

    assert [item.name for item in entries] == ["a-dir", "b-dir"]
    assert total_pages == 1


def test_list_directory_page_hides_dot_prefixed_directories(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / ".hidden-dir").mkdir(parents=True)
    (root / "visible-dir").mkdir()

    entries, _ = gateway.list_directory_page(root, root, page=0, page_size=10)

    assert [item.name for item in entries] == ["visible-dir"]


def test_build_gateway_submit_prompt_requires_telegram_feedback(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)

    prompt = gateway.build_gateway_submit_prompt(workdir, "fix the failing tests")

    assert "telegram_feedback" in prompt
    assert "interactive_feedback" in prompt
    assert str(workdir) in prompt
    assert "fix the failing tests" in prompt


def test_build_gateway_resume_prompt_requires_telegram_feedback(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)

    prompt = gateway.build_gateway_resume_prompt(workdir, "continue fixing tests")

    assert "telegram_feedback" in prompt
    assert "interactive_feedback" in prompt
    assert str(workdir) in prompt
    assert "Continue the previously resumed task" in prompt
    assert "continue fixing tests" in prompt


def test_build_gateway_commands_include_dynamic_away_description(monkeypatch):
    monkeypatch.setattr(gateway, "is_user_away", lambda: False)

    commands = gateway.build_gateway_commands()

    assert commands == [
        {"command": "done", "description": "Submit feedback"},
        {"command": "away", "description": "Mark yourself away from the computer"},
        {"command": "steer", "description": "Send follow-up input to a loaded session"},
        {"command": "submit", "description": "Run Codex in a selected directory"},
        {"command": "resume", "description": "Continue a saved Codex session"},
        {"command": "sessions", "description": "List saved Codex sessions"},
        {"command": "tasks", "description": "List loaded Codex sessions"},
    ]

    monkeypatch.setattr(gateway, "is_user_away", lambda: True)

    commands = gateway.build_gateway_commands()

    assert commands[1] == {
        "command": "away",
        "description": "Mark yourself back at the computer",
    }


@pytest.mark.asyncio
async def test_ensure_gateway_commands_registers_submit_when_missing():
    client = FakeGatewayClient()

    await gateway.ensure_gateway_commands(client)

    assert client.set_commands_calls == [gateway.build_gateway_commands()]


@pytest.mark.asyncio
async def test_ensure_gateway_commands_drops_obsolete_cancel_command():
    client = FakeGatewayClient()
    gateway_is_user_away = gateway.is_user_away
    gateway.is_user_away = lambda: False

    async def fake_get_commands():
        return [
            {"command": "done", "description": "Submit feedback"},
            {"command": "cancel", "description": "Cancel feedback"},
            {"command": "help", "description": "Show help"},
        ]

    client.get_commands = fake_get_commands

    try:
        await gateway.ensure_gateway_commands(client)
    finally:
        gateway.is_user_away = gateway_is_user_away

    assert client.set_commands_calls == [
        [
            {"command": "done", "description": "Submit feedback"},
            {"command": "help", "description": "Show help"},
            {"command": "away", "description": "Mark yourself away from the computer"},
            {
                "command": "steer",
                "description": "Send follow-up input to a loaded session",
            },
            {"command": "submit", "description": "Run Codex in a selected directory"},
            {"command": "resume", "description": "Continue a saved Codex session"},
            {"command": "sessions", "description": "List saved Codex sessions"},
            {"command": "tasks", "description": "List loaded Codex sessions"},
        ]
    ]


@pytest.mark.asyncio
async def test_handle_away_without_argument_toggles_current_state(tmp_path, monkeypatch):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    state = {"away": False}

    def fake_is_user_away():
        return state["away"]

    def fake_set_user_away(enabled: bool):
        state["away"] = enabled

    monkeypatch.setattr(gateway, "is_user_away", fake_is_user_away)
    monkeypatch.setattr(gateway, "set_user_away", fake_set_user_away)

    await service.handle_message({"chat": {"id": 123}, "text": "/away"})

    assert client.send_calls[-1]["chat_id"] == "123"
    assert state["away"] is True
    assert "Away mode enabled" in client.send_calls[-1]["text"]
    assert client.set_commands_calls[-1][1] == {
        "command": "away",
        "description": "Mark yourself back at the computer",
    }


@pytest.mark.asyncio
async def test_handle_away_on_enables_away_mode(tmp_path, monkeypatch):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    calls = []

    def fake_set_user_away(enabled: bool):
        calls.append(enabled)

    monkeypatch.setattr(gateway, "set_user_away", fake_set_user_away)

    await service.handle_message({"chat": {"id": 123}, "text": "/away on"})

    assert calls == [True]
    assert "Away mode enabled" in client.send_calls[-1]["text"]
    assert "telegram_feedback" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_message_routes_pending_feedback_before_steering_job(
    tmp_path, monkeypatch
):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    forwarded_messages = []
    steered_prompts = []

    running_job = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root,
        prompt="fix bug",
        requested_action="submit",
        latest_status="running",
    )
    service.active_jobs["123"] = running_job

    async def fake_handle_pending_feedback_message(message, feedback_client):
        forwarded_messages.append((message, feedback_client))
        return True

    async def fake_steer_running_job(job, prompt):
        steered_prompts.append(prompt)

    monkeypatch.setattr(
        gateway,
        "handle_pending_feedback_message",
        fake_handle_pending_feedback_message,
        raising=False,
    )
    monkeypatch.setattr(service, "_steer_running_job", fake_steer_running_job)

    await service.handle_message({"chat": {"id": 123}, "text": "feedback text"})

    assert steered_prompts == []
    assert forwarded_messages == [
        (
            {"chat": {"id": 123}, "text": "feedback text"},
            client,
        )
    ]


@pytest.mark.asyncio
async def test_handle_away_off_disables_away_mode(tmp_path, monkeypatch):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    calls = []

    def fake_set_user_away(enabled: bool):
        calls.append(enabled)

    monkeypatch.setattr(gateway, "set_user_away", fake_set_user_away)

    await service.handle_message({"chat": {"id": 123}, "text": "/away off"})

    assert calls == [False]
    assert "Away mode disabled" in client.send_calls[-1]["text"]
    assert "interactive_feedback" in client.send_calls[-1]["text"]


def test_start_submit_session_begins_at_default_root():
    state = gateway.start_submit_session(chat_id="123")

    assert state.chat_id == "123"
    assert state.mode == "browsing_directory"
    assert state.current_directory == gateway.DEFAULT_DIRECTORY_ROOT
    assert state.page == 0


def test_select_directory_moves_submit_session_to_waiting_prompt(tmp_path):
    selected = tmp_path / "home" / "kube" / "project"
    selected.mkdir(parents=True)
    state = gateway.start_submit_session(chat_id="123", root=selected.parent)

    updated_state = gateway.select_directory(state, selected)

    assert updated_state.mode == "waiting_for_task_prompt"
    assert updated_state.selected_directory == selected.resolve()


def test_build_directory_keyboard_includes_navigation_and_actions(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "project-a").mkdir(parents=True)
    (root / "project-b").mkdir()

    keyboard = gateway.build_directory_keyboard(root, root, page=0, page_size=10)

    flat_buttons = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
    assert "project-a" in flat_buttons
    assert "project-b" in flat_buttons
    assert "Select current directory" in flat_buttons
    assert "Cancel" in flat_buttons


@pytest.mark.asyncio
async def test_handle_submit_starts_directory_browser(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "project-a").mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)

    await service.handle_message(
        {"chat": {"id": 123}, "text": "/submit"},
    )

    assert client.send_calls
    assert "Select a working directory" in client.send_calls[0]["text"]
    assert service.submit_sessions["123"].mode == "browsing_directory"
    assert service.submit_sessions["123"].browser_message_id == 101


@pytest.mark.asyncio
async def test_handle_resume_lists_saved_sessions(tmp_path):
    root = tmp_path / "home" / "kube"
    session_dir = root / "project-a"
    session_dir.mkdir(parents=True)
    client = FakeGatewayClient()

    async def fake_list_threads():
        return [build_thread_summary(session_dir)]

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        list_codex_threads=fake_list_threads,
    )

    await service.handle_message(
        {"chat": {"id": 123}, "text": "/resume"},
    )

    assert client.send_calls
    assert "Select a saved Codex session" in client.send_calls[0]["text"]
    assert service.submit_sessions["123"].mode == "browsing_saved_sessions"
    assert service.submit_sessions["123"].requested_action == "resume"


@pytest.mark.asyncio
async def test_handle_callback_open_moves_into_selected_subdirectory(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "project-a" / "src").mkdir(parents=True)
    (root / "project-b").mkdir()
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)

    await service.handle_message({"chat": {"id": 123}, "text": "/submit"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "sub:open:0",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert service.submit_sessions["123"].current_directory == (root / "project-a").resolve()
    assert client.edit_calls
    assert client.answer_calls == [{"callback_query_id": "cb-1", "text": None}]


@pytest.mark.asyncio
async def test_handle_callback_out_of_range_does_not_crash_gateway(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "project-a").mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)

    await service.handle_message({"chat": {"id": 123}, "text": "/submit"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "sub:open:9",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert service.submit_sessions["123"].current_directory == root.resolve()
    assert client.answer_calls == [{"callback_query_id": "cb-1", "text": "This view is stale. Please try again."}]


@pytest.mark.asyncio
async def test_handle_callback_select_moves_to_waiting_prompt(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "project-a").mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)

    await service.handle_message({"chat": {"id": 123}, "text": "/submit"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "sub:select",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert service.submit_sessions["123"].mode == "waiting_for_task_prompt"
    assert service.submit_sessions["123"].selected_directory == root.resolve()
    assert "Send the Codex task" in client.edit_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_prompt_starts_codex_job_and_sends_started_message(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "project-a").mkdir(parents=True)
    client = FakeGatewayClient()
    launched = {}

    async def fake_launch(request):
        launched["request"] = request

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        launch_codex_job=fake_launch,
        feedback_mcp_available=_async_true,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/submit"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "sub:select",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )
    await service.handle_message({"chat": {"id": 123}, "text": "fix tests"})

    assert launched["request"].chat_id == "123"
    assert launched["request"].selected_directory == root.resolve()
    assert launched["request"].prompt == "fix tests"
    assert launched["request"].thread_id is None
    assert service.submit_sessions["123"].mode == "running_codex_job"
    assert "Task started" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_resume_without_saved_sessions_reports_empty_state(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    async def fake_list_threads():
        return []

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        list_codex_threads=fake_list_threads,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/resume"})

    assert "No resumable Codex sessions" in client.send_calls[-1]["text"]
    assert "123" not in service.submit_sessions


@pytest.mark.asyncio
async def test_handle_resume_select_moves_to_waiting_prompt(tmp_path):
    root = tmp_path / "home" / "kube"
    session_dir = root / "repo"
    session_dir.mkdir(parents=True)
    client = FakeGatewayClient()
    saved_session = build_thread_summary(session_dir)

    async def fake_list_threads():
        return [saved_session]

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        list_codex_threads=fake_list_threads,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/resume"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "ses:pick:0",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert service.submit_sessions["123"].mode == "waiting_for_task_prompt"
    assert service.submit_sessions["123"].requested_action == "resume"
    assert service.submit_sessions["123"].selected_directory == session_dir.resolve()
    assert service.submit_sessions["123"].selected_thread_id == saved_session.thread_id
    assert "Send the resume prompt" in client.edit_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_resume_prompt_starts_codex_reply_job_and_sends_started_message(
    tmp_path,
):
    root = tmp_path / "home" / "kube"
    session_dir = root / "repo"
    session_dir.mkdir(parents=True)
    client = FakeGatewayClient()
    launched = {}
    saved_session = build_thread_summary(session_dir, thread_id="thread-42")

    async def fake_list_threads():
        return [saved_session]

    async def fake_launch(request):
        launched["request"] = request

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        list_codex_threads=fake_list_threads,
        launch_codex_job=fake_launch,
        feedback_mcp_available=_async_true,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/resume"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "ses:pick:0",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )
    await service.handle_message({"chat": {"id": 123}, "text": "continue fixing tests"})

    assert launched["request"].chat_id == "123"
    assert launched["request"].selected_directory == session_dir.resolve()
    assert launched["request"].prompt == "continue fixing tests"
    assert launched["request"].thread_id == "thread-42"
    assert service.submit_sessions["123"].mode == "running_codex_job"
    assert "Resume started" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_prompt_blocks_submit_when_feedback_mcp_is_unavailable(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    launched = {"called": False}

    async def fake_launch(request):
        launched["called"] = True

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        launch_codex_job=fake_launch,
        feedback_mcp_available=_async_false,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/submit"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "sub:select",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )
    await service.handle_message({"chat": {"id": 123}, "text": "fix tests"})

    assert launched["called"] is False
    assert "telegram_feedback" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_sessions_lists_saved_codex_sessions(tmp_path):
    root = tmp_path / "home" / "kube"
    first_dir = root / "repo-a"
    second_dir = root / "repo-b"
    first_dir.mkdir(parents=True)
    second_dir.mkdir(parents=True)
    client = FakeGatewayClient()
    sessions = [
        build_thread_summary(
            first_dir,
            thread_id="thread-1",
            display_name="First task",
            updated_at=10,
        ),
        build_thread_summary(
            second_dir,
            thread_id="thread-2",
            display_name="Second task",
            updated_at=20,
        ),
    ]

    async def fake_list_threads():
        return sorted(sessions, key=lambda item: item.updated_at or 0, reverse=True)

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        list_codex_threads=fake_list_threads,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/sessions"})

    text = client.send_calls[-1]["text"]
    assert "Saved Codex sessions" in text
    assert text.index("Second task") < text.index("First task")
    assert "thread-2" in text


@pytest.mark.asyncio
async def test_handle_sessions_uses_interactive_pagination(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    sessions = []
    for index in range(8):
        session_dir = root / f"repo-{index}" / ("nested-" * 20)
        session_dir.mkdir(parents=True)
        sessions.append(
            build_thread_summary(
                session_dir,
                thread_id=f"thread-{index}",
                display_name=f"Session {index} " + ("very-long-name-" * 12),
                updated_at=index,
            )
        )

    async def fake_list_threads():
        return sessions

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=20,
        list_codex_threads=fake_list_threads,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/sessions"})

    first_message = client.send_calls[-1]
    first_buttons = [
        button["text"]
        for row in first_message["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "Saved Codex sessions" in first_message["text"]
    assert "Page 1/" in first_message["text"]
    assert "Next page" in first_buttons
    assert "Close" in first_buttons

    await service.handle_callback_query(
        {
            "id": "cb-sessions-1",
            "data": "ses:list:page:+1",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    edited_message = client.edit_calls[-1]
    edited_buttons = [
        button["text"]
        for row in edited_message["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "Page 2/" in edited_message["text"]
    assert "thread-7" in edited_message["text"]
    assert "Previous page" in edited_buttons


@pytest.mark.asyncio
async def test_handle_sessions_reports_error_to_telegram_instead_of_raising(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()

    async def fail_list_threads():
        raise RuntimeError("cannot list sessions")

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        list_codex_threads=fail_list_threads,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/sessions"})

    assert "Telegram gateway error" in client.send_calls[-1]["text"]
    assert "cannot list sessions" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_tasks_lists_loaded_sessions(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    first_dir = root / "repo-a"
    second_dir = root / "repo-b"
    first_dir.mkdir()
    second_dir.mkdir()
    service.loaded_sessions = {
        "123": [
            gateway.RunningCodexJob(
                chat_id="123",
                selected_directory=first_dir.resolve(),
                prompt="first prompt",
                requested_action="submit",
                handle=object(),
                process=object(),
                latest_status="idle",
                latest_message="",
                usage_summary=None,
                thread_id="thread-1",
                session_title="First loaded session",
            ),
            gateway.RunningCodexJob(
                chat_id="123",
                selected_directory=second_dir.resolve(),
                prompt="second prompt",
                requested_action="resume",
                handle=object(),
                process=object(),
                latest_status="active",
                latest_message="working",
                usage_summary=None,
                thread_id="thread-2",
                session_title="Second loaded session",
            ),
        ]
    }

    await service.handle_message({"chat": {"id": 123}, "text": "/tasks"})

    text = client.send_calls[-1]["text"]
    assert "Loaded Codex sessions" in text
    assert "Second loaded session" in text
    assert "First loaded session" in text
    assert "thread-2" in text


@pytest.mark.asyncio
async def test_handle_task_alias_lists_loaded_sessions(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    repo_dir = root / "repo-a"
    repo_dir.mkdir()
    service.loaded_sessions = {
        "123": [
            gateway.RunningCodexJob(
                chat_id="123",
                selected_directory=repo_dir.resolve(),
                prompt="fix tests",
                requested_action="submit",
                handle=object(),
                process=object(),
                latest_status="idle",
                latest_message="",
                usage_summary=None,
                thread_id="thread-1",
                session_title="Loaded session",
            )
        ]
    }

    await service.handle_message({"chat": {"id": 123}, "text": "/task"})

    assert "Loaded Codex sessions" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_steer_collects_text_then_shows_loaded_session_picker(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    repo_dir = root / "repo-a"
    repo_dir.mkdir()
    service.loaded_sessions = {
        "123": [
            gateway.RunningCodexJob(
                chat_id="123",
                selected_directory=repo_dir.resolve(),
                prompt="fix tests",
                requested_action="submit",
                handle=object(),
                process=object(),
                latest_status="idle",
                latest_message="",
                usage_summary=None,
                thread_id="thread-1",
                session_title="Loaded session",
            )
        ]
    }

    await service.handle_message({"chat": {"id": 123}, "text": "/steer"})
    assert "Send the steer text" in client.send_calls[-1]["text"]

    await service.handle_message({"chat": {"id": 123}, "text": "please continue from here"})
    await service.handle_message({"chat": {"id": 123}, "text": "/done"})

    text = client.send_calls[-1]["text"]
    buttons = [
        button["text"]
        for row in client.send_calls[-1]["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "Select a loaded session to steer" in text
    assert "Loaded session" in text
    assert "Close" in buttons


@pytest.mark.asyncio
async def test_handle_steer_selection_dispatches_prompt_to_loaded_session(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    repo_dir = root / "repo-a"
    repo_dir.mkdir()
    loaded_job = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=repo_dir.resolve(),
        prompt="fix tests",
        requested_action="submit",
        handle=object(),
        process=object(),
        latest_status="idle",
        latest_message="",
        usage_summary=None,
        thread_id="thread-1",
        session_title="Loaded session",
    )
    service.loaded_sessions = {"123": [loaded_job]}
    captured = {}

    async def fake_dispatch(job, prompt):
        captured["job"] = job
        captured["prompt"] = prompt

    service._dispatch_steer_to_loaded_session = fake_dispatch

    await service.handle_message({"chat": {"id": 123}, "text": "/steer"})
    await service.handle_message({"chat": {"id": 123}, "text": "please continue from here"})
    await service.handle_message({"chat": {"id": 123}, "text": "/done"})
    await service.handle_callback_query(
        {
            "id": "cb-steer-1",
            "data": "steer:pick:0",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert captured["job"] is loaded_job
    assert captured["prompt"] == "please continue from here"


@pytest.mark.asyncio
async def test_handle_plain_text_steers_active_codex_turn(tmp_path):
    class SteerableProcess:
        def __init__(self):
            self.calls = []

        async def steer_turn(self, prompt, *, thread_id=None, turn_id=None):
            self.calls.append(
                {
                    "prompt": prompt,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                }
            )
            return turn_id

    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    process = SteerableProcess()
    service.active_jobs["123"] = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root.resolve(),
        prompt="fix tests",
        requested_action="submit",
        handle=object(),
        process=process,
        latest_status="Running",
        latest_message="",
        usage_summary=None,
        thread_id="thread-1",
        turn_id="turn-1",
    )

    await service.handle_message({"chat": {"id": 123}, "text": "actually focus on tests first"})

    assert process.calls == [
        {
            "prompt": "actually focus on tests first",
            "thread_id": "thread-1",
            "turn_id": "turn-1",
        }
    ]
    assert service.active_jobs["123"].latest_status == "Steer sent"
    assert service.active_jobs["123"].latest_message == "actually focus on tests first"
    assert "Follow-up sent to the active Codex turn." in client.send_calls[-1]["text"]


def test_apply_codex_event_updates_job_status_thread_and_usage(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    job = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root.resolve(),
        prompt="fix tests",
        requested_action="submit",
        handle=object(),
        process=None,
        latest_status="Starting",
        latest_message="",
        usage_summary=None,
    )

    gateway.apply_codex_event(
        job,
        {
            "type": "session_configured",
            "session_id": "thread-1",
        },
    )
    gateway.apply_codex_event(
        job,
        {
            "type": "task_started",
            "turn_id": "turn-1",
        },
    )
    gateway.apply_codex_event(
        job,
        {
            "type": "agent_message",
            "message": "Inspecting tests",
        },
    )
    gateway.apply_codex_event(
        job,
        {
            "type": "token_count",
            "info": {
                "tokenUsage": {
                    "total": {
                        "inputTokens": 11,
                        "outputTokens": 7,
                        "reasoningOutputTokens": 3,
                    }
                }
            },
        },
    )

    assert job.latest_status == "agent_message"
    assert job.thread_id == "thread-1"
    assert job.turn_id == "turn-1"
    assert job.latest_message == "Inspecting tests"
    assert job.usage_summary == "input 11 / output 7 / reasoning 3"


def test_completion_message_includes_usage(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)

    message = gateway.TelegramGatewayService._completion_message(
        True,
        "All done",
        workdir,
        "input 11 / output 7",
    )

    assert "Task finished successfully." in message
    assert "All done" in message
    assert "Usage:\ninput 11 / output 7" in message


@pytest.mark.asyncio
async def test_handle_task_stop_callback_cancels_running_job(tmp_path):
    class CancellableProcess:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    class CancellableHandle:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    handle = CancellableHandle()
    process = CancellableProcess()
    service.active_jobs["123"] = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root.resolve(),
        prompt="fix tests",
        requested_action="submit",
        handle=handle,
        process=process,
        latest_status="Running",
        latest_message="",
        usage_summary=None,
    )

    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "task:stop",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert process.terminated is True
    assert handle.cancelled is False
    assert service.active_jobs["123"].termination_requested is True
    assert service.active_jobs["123"].latest_status == "Termination requested"
    assert client.edit_calls[-1]["text"] == "Task termination requested."


@pytest.mark.asyncio
async def test_handle_completion_confirmation_callback_resolves_pending_request(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    request = completion_confirmation.create_completion_confirmation_request(
        chat_id="123",
        project_directory=str(root),
        summary="Finished the requested task.",
    )
    completion_confirmation.register_completion_confirmation_message(
        request.request_id,
        101,
    )

    await service.handle_callback_query(
        {
            "id": "cb-confirm-1",
            "data": f"tcc:approve:{request.request_id}",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    decision = await completion_confirmation.wait_for_completion_confirmation(
        request,
        timeout=1,
    )

    assert decision["approved"] is True
    assert decision["decision"] == "approved"
    assert "approved" in client.edit_calls[-1]["text"].lower()


def test_detect_completion_confirmation_state_handles_missing_rejected_and_approved():
    missing_thread = {
        "thread": {
            "turns": [
                {
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "Done"}],
                }
            ]
        }
    }
    rejected_thread = {
        "thread": {
            "turns": [
                {
                    "status": "completed",
                    "items": [
                        {
                            "type": "mcpToolCall",
                            "tool": "telegram_confirm_completion",
                            "status": "completed",
                            "result": {"approved": False, "decision": "rejected"},
                        }
                    ],
                }
            ]
        }
    }
    approved_thread = {
        "thread": {
            "turns": [
                {
                    "status": "completed",
                    "items": [
                        {
                            "type": "mcpToolCall",
                            "tool": "telegram_confirm_completion",
                            "status": "completed",
                            "result": {"approved": True, "decision": "approved"},
                        }
                    ],
                }
            ]
        }
    }

    assert gateway.detect_completion_confirmation_state(missing_thread) == "missing"
    assert gateway.detect_completion_confirmation_state(rejected_thread) == "rejected"
    assert gateway.detect_completion_confirmation_state(approved_thread) == "approved"


@pytest.mark.asyncio
async def test_run_turn_with_completion_guard_retries_when_confirmation_missing(
    tmp_path,
    monkeypatch,
):
    class FakeProcess:
        def __init__(self):
            self.prompts = []

        async def run_turn(
            self,
            *,
            cwd,
            prompt,
            thread_id=None,
            on_event=None,
            on_request_user_input=None,
        ):
            self.prompts.append(prompt)
            turn_index = len(self.prompts)
            return codex_app_server.CodexTurnResult(
                thread_id="thread-1",
                turn_id=f"turn-{turn_index}",
                content=f"turn {turn_index}",
                status="completed",
            )

        async def read_thread(self, thread_id, *, include_turns=False):
            assert include_turns is True
            if len(self.prompts) == 1:
                return {
                    "thread": {
                        "turns": [
                            {
                                "status": "completed",
                                "items": [{"type": "agentMessage", "text": "Done"}],
                            }
                        ]
                    }
                }
            return {
                "thread": {
                    "turns": [
                        {
                            "status": "completed",
                            "items": [
                                {
                                    "type": "mcpToolCall",
                                    "tool": "telegram_confirm_completion",
                                    "status": "completed",
                                    "result": {
                                        "approved": True,
                                        "decision": "approved",
                                    },
                                }
                            ],
                        }
                    ]
                }
            }

    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    process = FakeProcess()
    job = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root.resolve(),
        prompt="finish the task",
        requested_action="submit",
        handle=object(),
        process=process,
        latest_status="Running",
        latest_message="",
        usage_summary=None,
        thread_id="thread-1",
        session_title="Loaded session",
    )
    launch_request = gateway.CodexLaunchRequest(
        chat_id="123",
        requested_action="submit",
        selected_directory=root.resolve(),
        prompt="finish the task",
        thread_id="thread-1",
        session_title="Loaded session",
    )

    monkeypatch.setattr(gateway, "is_user_away", lambda: True)

    result = await service._run_turn_with_completion_guard(
        running_job=job,
        process=process,
        launch_request=launch_request,
        prompt="finish the task",
    )

    assert result.turn_id == "turn-2"
    assert len(process.prompts) == 2
    assert "telegram_confirm_completion" in process.prompts[1]
    assert "must not finish" in process.prompts[1].lower()


@pytest.mark.asyncio
async def test_poll_updates_retries_after_bad_gateway(monkeypatch):
    calls = {"count": 0}
    sleep_calls = []

    class FakePollingClient:
        async def get_updates(self, offset=None, timeout=30):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TelegramClientError("Bad Gateway")
            return [{"update_id": 1}]

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(gateway.asyncio, "sleep", fake_sleep)

    updates = await gateway.poll_updates_with_retry(
        FakePollingClient(),
        offset=7,
        timeout=15,
        retry_delay=3,
    )

    assert updates == [{"update_id": 1}]
    assert calls["count"] == 2
    assert sleep_calls == [3]


async def _async_true():
    return True


async def _async_false():
    return False
