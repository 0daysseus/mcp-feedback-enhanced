#!/usr/bin/env python3
"""
Telegram gateway state and Codex command tests.
"""

from pathlib import Path

import pytest

from mcp_feedback_enhanced.telegram import gateway


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


def test_build_codex_exec_command_for_git_directory(tmp_path):
    workdir = tmp_path / "repo"
    (workdir / ".git").mkdir(parents=True)

    command = gateway.build_codex_exec_command(workdir, "fix tests")

    assert command == [
        "codex",
        "exec",
        "--full-auto",
        "--json",
        "-C",
        str(workdir),
        "fix tests",
    ]


def test_build_codex_exec_command_adds_skip_git_check_for_non_repo(tmp_path):
    workdir = tmp_path / "plain-dir"
    workdir.mkdir(parents=True)

    command = gateway.build_codex_exec_command(workdir, "fix tests")

    assert command == [
        "codex",
        "exec",
        "--full-auto",
        "--json",
        "-C",
        str(workdir),
        "--skip-git-repo-check",
        "fix tests",
    ]


def test_build_codex_exec_command_supports_output_file(tmp_path):
    workdir = tmp_path / "repo"
    output_file = tmp_path / "codex-last-message.txt"
    (workdir / ".git").mkdir(parents=True)

    command = gateway.build_codex_exec_command(
        workdir,
        "fix tests",
        output_file=output_file,
    )

    assert command == [
        "codex",
        "exec",
        "--full-auto",
        "--json",
        "-C",
        str(workdir),
        "-o",
        str(output_file),
        "fix tests",
    ]


def test_build_gateway_submit_prompt_requires_telegram_feedback(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)

    prompt = gateway.build_gateway_submit_prompt(workdir, "fix the failing tests")

    assert "telegram_feedback" in prompt
    assert "interactive_feedback" in prompt
    assert str(workdir) in prompt
    assert "fix the failing tests" in prompt


def test_build_codex_resume_command_for_git_directory(tmp_path):
    workdir = tmp_path / "repo"
    (workdir / ".git").mkdir(parents=True)

    command = gateway.build_codex_resume_command(
        workdir,
        gateway.build_gateway_resume_prompt(workdir, "continue fixing tests"),
    )

    assert command == [
        "codex",
        "exec",
        "resume",
        "--last",
        "--full-auto",
        "--json",
        gateway.build_gateway_resume_prompt(workdir, "continue fixing tests"),
    ]


def test_build_codex_resume_command_adds_skip_git_check_for_non_repo(tmp_path):
    workdir = tmp_path / "plain-dir"
    workdir.mkdir(parents=True)

    command = gateway.build_codex_resume_command(
        workdir,
        gateway.build_gateway_resume_prompt(workdir, "continue fixing tests"),
    )

    assert command == [
        "codex",
        "exec",
        "resume",
        "--last",
        "--full-auto",
        "--json",
        "--skip-git-repo-check",
        gateway.build_gateway_resume_prompt(workdir, "continue fixing tests"),
    ]


def test_build_gateway_resume_prompt_requires_telegram_feedback(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)

    prompt = gateway.build_gateway_resume_prompt(workdir, "continue fixing tests")

    assert "telegram_feedback" in prompt
    assert "interactive_feedback" in prompt
    assert str(workdir) in prompt
    assert "Continue the previously resumed task" in prompt
    assert "continue fixing tests" in prompt


def test_gateway_required_commands_include_submit():
    assert gateway.GATEWAY_TELEGRAM_COMMANDS == [
        {"command": "done", "description": "Submit feedback"},
        {"command": "cancel", "description": "Cancel feedback"},
        {"command": "submit", "description": "Run Codex in a selected directory"},
        {"command": "resume", "description": "Resume the latest Codex task in a directory"},
        {"command": "tasks", "description": "List and manage running Codex tasks"},
    ]


@pytest.mark.asyncio
async def test_ensure_gateway_commands_registers_submit_when_missing():
    client = FakeGatewayClient()

    await gateway.ensure_gateway_commands(client)

    assert client.set_commands_calls == [gateway.GATEWAY_TELEGRAM_COMMANDS]


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
async def test_handle_resume_starts_directory_browser_in_resume_mode(tmp_path):
    root = tmp_path / "home" / "kube"
    (root / "project-a").mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)

    await service.handle_message(
        {"chat": {"id": 123}, "text": "/resume"},
    )

    assert client.send_calls
    assert "Select a working directory" in client.send_calls[0]["text"]
    assert service.submit_sessions["123"].mode == "browsing_directory"
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

    async def fake_launch(chat_id, selected_directory, prompt):
        launched["chat_id"] = chat_id
        launched["selected_directory"] = selected_directory
        launched["prompt"] = prompt

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        launch_codex_job=fake_launch,
        feedback_mcp_available=lambda: _async_true(),
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

    assert launched == {
        "chat_id": "123",
        "selected_directory": root.resolve(),
        "prompt": "fix tests",
    }
    assert service.submit_sessions["123"].mode == "running_codex_job"
    assert "Task started" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_resume_select_moves_to_waiting_prompt(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)

    await service.handle_message({"chat": {"id": 123}, "text": "/resume"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "sub:select",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert service.submit_sessions["123"].mode == "waiting_for_task_prompt"
    assert service.submit_sessions["123"].requested_action == "resume"
    assert service.submit_sessions["123"].selected_directory == root.resolve()
    assert "Send the resume prompt" in client.edit_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_resume_prompt_starts_codex_job_and_sends_started_message(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    launched = {}

    async def fake_launch(chat_id, selected_directory, prompt):
        launched["chat_id"] = chat_id
        launched["selected_directory"] = selected_directory
        launched["prompt"] = prompt

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        launch_codex_job=fake_launch,
        feedback_mcp_available=lambda: _async_true(),
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/resume"})
    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "sub:select",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )
    await service.handle_message({"chat": {"id": 123}, "text": "continue fixing tests"})

    assert launched == {
        "chat_id": "123",
        "selected_directory": root.resolve(),
        "prompt": "continue fixing tests",
    }
    assert service.submit_sessions["123"].mode == "running_codex_job"
    assert "Resume started" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_prompt_blocks_submit_when_feedback_mcp_is_unavailable(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    launched = {"called": False}

    async def fake_launch(chat_id, selected_directory, prompt):
        launched["called"] = True

    service = gateway.TelegramGatewayService(
        client=client,
        root=root,
        page_size=10,
        launch_codex_job=fake_launch,
        feedback_mcp_available=lambda: _async_false(),
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
async def test_handle_tasks_lists_running_job_with_status_and_buttons(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    service.active_jobs["123"] = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root.resolve(),
        prompt="fix tests",
        requested_action="submit",
        handle=object(),
        process=None,
        latest_status="Running tests",
        latest_message="Investigating failures",
        usage_summary=None,
    )

    await service.handle_message({"chat": {"id": 123}, "text": "/tasks"})

    assert "Running tasks" in client.send_calls[-1]["text"]
    assert "Running tests" in client.send_calls[-1]["text"]
    assert client.send_calls[-1]["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "View latest status", "callback_data": "task:latest"}],
            [{"text": "Terminate current task", "callback_data": "task:stop"}],
        ]
    }


@pytest.mark.asyncio
async def test_handle_task_alias_lists_running_job_with_terminate_button(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    service.active_jobs["123"] = gateway.RunningCodexJob(
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

    await service.handle_message({"chat": {"id": 123}, "text": "/task"})

    assert "Running tasks" in client.send_calls[-1]["text"]


@pytest.mark.asyncio
async def test_handle_task_latest_callback_refreshes_status(tmp_path):
    root = tmp_path / "home" / "kube"
    root.mkdir(parents=True)
    client = FakeGatewayClient()
    service = gateway.TelegramGatewayService(client=client, root=root, page_size=10)
    service.active_jobs["123"] = gateway.RunningCodexJob(
        chat_id="123",
        selected_directory=root.resolve(),
        prompt="fix tests",
        requested_action="submit",
        handle=object(),
        process=None,
        latest_status="Applying patch",
        latest_message="Updating telegram gateway tests",
        usage_summary="input 10 / output 5",
    )

    await service.handle_callback_query(
        {
            "id": "cb-1",
            "data": "task:latest",
            "message": {"message_id": 101, "chat": {"id": 123}},
        }
    )

    assert "Applying patch" in client.edit_calls[-1]["text"]
    assert "Updating telegram gateway tests" in client.edit_calls[-1]["text"]
    assert "input 10 / output 5" in client.edit_calls[-1]["text"]


def test_apply_codex_event_updates_job_status_and_usage(tmp_path):
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
            "type": "agent_message",
            "message": {"content": [{"type": "output_text", "text": "Inspecting tests"}]},
        },
    )
    gateway.apply_codex_event(
        job,
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 11, "output_tokens": 7, "reasoning_tokens": 3},
        },
    )

    assert job.latest_status == "agent_message"
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


async def _async_true():
    return True


async def _async_false():
    return False
