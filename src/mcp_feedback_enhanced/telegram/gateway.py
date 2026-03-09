#!/usr/bin/env python3
"""
Telegram gateway helpers for directory browsing and Codex execution.
"""

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, replace
from math import ceil
from pathlib import Path
from typing import Awaitable, Callable

from .client import TelegramBotClient
from .session import DEFAULT_TELEGRAM_API_BASE


DEFAULT_DIRECTORY_ROOT = Path("/home/kube")
GATEWAY_TELEGRAM_COMMANDS = [
    {"command": "done", "description": "Submit feedback"},
    {"command": "cancel", "description": "Cancel feedback"},
    {"command": "submit", "description": "Run Codex in a selected directory"},
    {"command": "resume", "description": "Resume the latest Codex task in a directory"},
    {"command": "tasks", "description": "List and manage running Codex tasks"},
]


@dataclass(slots=True)
class SubmitSessionState:
    """In-memory chat-local state for Telegram submit flow."""

    chat_id: str
    mode: str
    requested_action: str
    root_directory: Path
    current_directory: Path
    page: int = 0
    selected_directory: Path | None = None
    browser_message_id: int | None = None


@dataclass(slots=True)
class RunningCodexJob:
    """Minimal in-memory record of a running Codex task."""

    chat_id: str
    selected_directory: Path
    prompt: str | None
    requested_action: str
    handle: object | None = None
    process: object | None = None
    latest_status: str = "Queued"
    latest_message: str = ""
    usage_summary: str | None = None
    termination_requested: bool = False


def run_gateway() -> None:
    """Run the Telegram gateway event loop."""
    asyncio.run(run_gateway_loop())


def resolve_directory(root: Path, candidate: Path) -> Path:
    """Resolve a candidate directory and reject paths outside the allowed root."""
    resolved_root = root.expanduser().resolve()
    resolved_candidate = candidate.expanduser().resolve()

    if (
        resolved_candidate != resolved_root
        and resolved_root not in resolved_candidate.parents
    ):
        raise ValueError("Selected path is outside the allowed root")

    return resolved_candidate


def list_directory_page(
    root: Path,
    current: Path,
    page: int,
    page_size: int,
) -> tuple[list[Path], int]:
    """Return one sorted page of directories within the current path."""
    resolved_current = resolve_directory(root, current)
    directories = sorted(
        [
            entry
            for entry in resolved_current.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
        ],
        key=lambda entry: entry.name,
    )

    total_pages = max(1, ceil(len(directories) / page_size))
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * page_size
    end = start + page_size
    return directories[start:end], total_pages


def build_directory_keyboard(
    root: Path,
    current: Path,
    page: int,
    page_size: int,
) -> dict[str, list[list[dict[str, str]]]]:
    """Build inline keyboard markup for directory browsing."""
    directories, total_pages = list_directory_page(root, current, page, page_size)
    resolved_root = resolve_directory(root, root)
    resolved_current = resolve_directory(root, current)

    inline_keyboard: list[list[dict[str, str]]] = [
        [{"text": entry.name, "callback_data": f"sub:open:{index}"}]
        for index, entry in enumerate(directories)
    ]

    navigation_row: list[dict[str, str]] = []
    if resolved_current != resolved_root:
        navigation_row.append({"text": "..", "callback_data": "sub:up"})
    if page > 0:
        navigation_row.append({"text": "Previous page", "callback_data": "sub:page:-1"})
    if page < total_pages - 1:
        navigation_row.append({"text": "Next page", "callback_data": "sub:page:+1"})
    if navigation_row:
        inline_keyboard.append(navigation_row)

    inline_keyboard.append(
        [
            {"text": "Select current directory", "callback_data": "sub:select"},
            {"text": "Cancel", "callback_data": "sub:cancel"},
        ]
    )
    return {"inline_keyboard": inline_keyboard}


def start_submit_session(
    chat_id: str,
    root: Path = DEFAULT_DIRECTORY_ROOT,
    requested_action: str = "submit",
) -> SubmitSessionState:
    """Create a new submit browsing session for the given chat."""
    resolved_root = resolve_directory(root, root)
    return SubmitSessionState(
        chat_id=chat_id,
        mode="browsing_directory",
        requested_action=requested_action,
        root_directory=resolved_root,
        current_directory=resolved_root,
        page=0,
    )


def select_directory(
    state: SubmitSessionState,
    selected_directory: Path,
) -> SubmitSessionState:
    """Move the submit session into prompt-waiting state for the selected directory."""
    resolved_selected = resolve_directory(state.root_directory, selected_directory)
    return SubmitSessionState(
        chat_id=state.chat_id,
        mode="waiting_for_task_prompt",
        requested_action=state.requested_action,
        root_directory=state.root_directory,
        current_directory=resolved_selected,
        page=state.page,
        selected_directory=resolved_selected,
        browser_message_id=state.browser_message_id,
    )


class TelegramGatewayService:
    """Chat-local submit flow manager for Telegram gateway mode."""

    def __init__(
        self,
        client: object,
        root: Path = DEFAULT_DIRECTORY_ROOT,
        page_size: int = 10,
        launch_codex_job: Callable[[str, Path, str], Awaitable[None]] | None = None,
        feedback_mcp_available: Callable[[], Awaitable[bool]] | None = None,
    ):
        self.client = client
        self.root = resolve_directory(root, root)
        self.page_size = page_size
        self.submit_sessions: dict[str, SubmitSessionState] = {}
        self.active_jobs: dict[str, RunningCodexJob] = {}
        self._launch_codex_job = launch_codex_job or self._launch_codex_job_impl
        self._feedback_mcp_available = (
            feedback_mcp_available or feedback_server_seems_configured
        )

    async def handle_message(self, message: dict) -> None:
        """Handle a Telegram message update."""
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id"))
        text = message.get("text")
        if not isinstance(text, str):
            return

        if text == "/submit":
            if chat_id in self.active_jobs and self._job_is_running(self.active_jobs[chat_id]):
                await self.client.send_message(
                    chat_id,
                    "A Codex task is already running for this chat. Wait for it to finish first.",
                )
                return
            await self._start_directory_browser(chat_id, requested_action="submit")
            return

        if text == "/resume":
            if chat_id in self.active_jobs and self._job_is_running(self.active_jobs[chat_id]):
                await self.client.send_message(
                    chat_id,
                    "A Codex task is already running for this chat. Wait for it to finish first.",
                )
                return
            await self._start_directory_browser(chat_id, requested_action="resume")
            return

        if text in {"/tasks", "/task"}:
            await self._show_running_tasks(chat_id)
            return

        if text == "/cancel":
            self.submit_sessions.pop(chat_id, None)
            await self.client.send_message(
                chat_id,
                "Current Telegram gateway session cancelled.",
            )
            return

        state = self.submit_sessions.get(chat_id)
        if state is None:
            return

        if state.mode == "waiting_for_task_prompt" and not text.startswith("/"):
            if not await self._feedback_mcp_available():
                await self.client.send_message(
                    chat_id,
                    f"Cannot start `/{state.requested_action}` because Codex does not appear to have an MCP server exposing `telegram_feedback`. Configure mcp-feedback-enhanced in Codex first.",
                )
                return
            await self._launch_codex_job(chat_id, state.selected_directory, text)
            self.submit_sessions[chat_id] = replace(state, mode="running_codex_job")
            if state.requested_action == "resume":
                await self.client.send_message(chat_id, "Resume started. Codex is working.")
            else:
                await self.client.send_message(chat_id, "Task started. Codex is working.")

    async def handle_callback_query(self, callback_query: dict) -> None:
        """Handle a Telegram callback query from inline buttons."""
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id"))
        data = callback_query.get("data")
        if not isinstance(data, str):
            return

        if data == "task:stop":
            running_job = self.active_jobs.get(chat_id)
            if running_job is not None:
                running_job.termination_requested = True
                running_job.latest_status = "Termination requested"
                if hasattr(running_job.process, "terminate"):
                    running_job.process.terminate()
                elif hasattr(running_job.handle, "cancel"):
                    running_job.handle.cancel()
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                "Task termination requested.",
                reply_markup=None,
            )
            await self.client.answer_callback_query(callback_query.get("id"), text=None)
            return

        if data == "task:latest":
            running_job = self.active_jobs.get(chat_id)
            if running_job is None:
                await self.client.edit_message_text(
                    chat_id,
                    message["message_id"],
                    "No running tasks.",
                    reply_markup=None,
                )
            else:
                await self.client.edit_message_text(
                    chat_id,
                    message["message_id"],
                    self._task_status_text(running_job),
                    reply_markup=self._task_keyboard(),
                )
            await self.client.answer_callback_query(callback_query.get("id"), text=None)
            return

        state = self.submit_sessions.get(chat_id)
        if state is None:
            await self.client.answer_callback_query(callback_query.get("id"), text=None)
            return

        if data.startswith("sub:open:"):
            index = int(data.rsplit(":", 1)[-1])
            entries, _ = list_directory_page(
                state.root_directory,
                state.current_directory,
                state.page,
                self.page_size,
            )
            if index < 0 or index >= len(entries):
                await self.client.answer_callback_query(
                    callback_query.get("id"),
                    text="This view is stale. Please try again.",
                )
                return
            selected = entries[index]
            updated_state = replace(state, current_directory=selected.resolve(), page=0)
            self.submit_sessions[chat_id] = updated_state
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                self._browser_text(updated_state.current_directory),
                reply_markup=build_directory_keyboard(
                    updated_state.root_directory,
                    updated_state.current_directory,
                    updated_state.page,
                    self.page_size,
                ),
            )
        elif data == "sub:up":
            parent = (
                state.current_directory.parent
                if state.current_directory != state.root_directory
                else state.root_directory
            )
            updated_state = replace(state, current_directory=parent.resolve(), page=0)
            self.submit_sessions[chat_id] = updated_state
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                self._browser_text(updated_state.current_directory),
                reply_markup=build_directory_keyboard(
                    updated_state.root_directory,
                    updated_state.current_directory,
                    updated_state.page,
                    self.page_size,
                ),
            )
        elif data.startswith("sub:page:"):
            delta = int(data.rsplit(":", 1)[-1])
            updated_state = replace(state, page=max(state.page + delta, 0))
            self.submit_sessions[chat_id] = updated_state
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                self._browser_text(updated_state.current_directory),
                reply_markup=build_directory_keyboard(
                    updated_state.root_directory,
                    updated_state.current_directory,
                    updated_state.page,
                    self.page_size,
                ),
            )
        elif data == "sub:select":
            updated_state = select_directory(state, state.current_directory)
            self.submit_sessions[chat_id] = updated_state
            prompt_label = (
                "Send the resume prompt for"
                if updated_state.requested_action == "resume"
                else "Send the Codex task for"
            )
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                f"{prompt_label}:\n{updated_state.selected_directory}",
                reply_markup=None,
            )
        elif data == "sub:cancel":
            self.submit_sessions.pop(chat_id, None)
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                "Telegram submit flow cancelled.",
                reply_markup=None,
            )
        await self.client.answer_callback_query(callback_query.get("id"), text=None)

    async def _start_directory_browser(
        self,
        chat_id: str,
        requested_action: str,
    ) -> None:
        state = start_submit_session(
            chat_id=chat_id,
            root=self.root,
            requested_action=requested_action,
        )
        result = await self.client.send_message(
            chat_id,
            self._browser_text(state.current_directory),
            reply_markup=build_directory_keyboard(
                state.root_directory,
                state.current_directory,
                state.page,
                self.page_size,
            ),
        )
        self.submit_sessions[chat_id] = replace(
            state,
            browser_message_id=result.get("message_id"),
        )

    async def _show_running_tasks(self, chat_id: str) -> None:
        running_job = self.active_jobs.get(chat_id)
        if running_job is None:
            await self.client.send_message(chat_id, "No running tasks.")
            return

        await self.client.send_message(
            chat_id,
            self._task_status_text(running_job),
            reply_markup=self._task_keyboard(),
        )

    @staticmethod
    def _browser_text(current_directory: Path) -> str:
        return f"Select a working directory under /home/kube:\n{current_directory}"

    @staticmethod
    def _task_keyboard() -> dict[str, list[list[dict[str, str]]]]:
        return {
            "inline_keyboard": [
                [{"text": "View latest status", "callback_data": "task:latest"}],
                [{"text": "Terminate current task", "callback_data": "task:stop"}],
            ]
        }

    @staticmethod
    def _task_status_text(job: RunningCodexJob) -> str:
        title = "Running tasks:"
        task_name = job.prompt if job.prompt is not None else "Resume latest session"
        lines = [
            title,
            "",
            f"Task: {task_name}",
            f"Mode: {job.requested_action}",
            f"Directory: {job.selected_directory}",
            f"Status: {job.latest_status}",
        ]
        if job.latest_message:
            lines.extend(["", "Latest update:", job.latest_message])
        if job.usage_summary:
            lines.extend(["", "Usage:", job.usage_summary])
        return "\n".join(lines)

    @staticmethod
    def _completion_message(
        success: bool,
        output_text: str,
        workdir: Path,
        usage_summary: str | None = None,
    ) -> str:
        status = "Task finished successfully." if success else "Task failed."
        details = output_text.strip() or "No final Codex output was captured."
        message = f"{status}\n\nDirectory:\n{workdir}\n\nResult:\n{details}"
        if usage_summary:
            message = f"{message}\n\nUsage:\n{usage_summary}"
        return message

    async def _launch_codex_job_impl(
        self,
        chat_id: str,
        selected_directory: Path | None,
        prompt: str | None,
    ) -> None:
        if selected_directory is None:
            await self.client.send_message(chat_id, "No working directory was selected.")
            return

        state = self.submit_sessions.get(chat_id)
        requested_action = (
            state.requested_action if state is not None else "submit"
        )
        running_job = RunningCodexJob(
            chat_id=chat_id,
            selected_directory=selected_directory,
            prompt=prompt,
            requested_action=requested_action,
            latest_status="Starting Codex",
        )
        task = asyncio.create_task(
            self._run_codex_job(chat_id, selected_directory, prompt)
        )
        running_job.handle = task
        self.active_jobs[chat_id] = running_job

    async def _run_codex_job(
        self,
        chat_id: str,
        selected_directory: Path,
        prompt: str | None,
    ) -> None:
        output_file_path = Path(
            tempfile.mkstemp(prefix="codex-last-", suffix=".txt")[1]
        )
        running_job = self.active_jobs.get(chat_id)
        spawn_cwd: str | None = None
        if running_job is not None and running_job.requested_action == "resume":
            command = build_codex_resume_command(
                selected_directory,
                build_gateway_resume_prompt(selected_directory, prompt or ""),
                output_file=output_file_path,
            )
            spawn_cwd = str(selected_directory.resolve())
        else:
            command = build_codex_exec_command(
                selected_directory,
                build_gateway_submit_prompt(selected_directory, prompt),
                output_file=output_file_path,
            )
        success = False
        result_text = ""
        stderr_lines: list[str] = []

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=spawn_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if running_job is not None:
                running_job.process = process
                running_job.latest_status = "Running Codex"

            stdout_task = asyncio.create_task(
                self._consume_codex_stdout(process.stdout, running_job)
            )
            stderr_task = asyncio.create_task(
                self._consume_codex_stderr(process.stderr, running_job, stderr_lines)
            )
            return_code = await process.wait()
            await stdout_task
            await stderr_task
            success = return_code == 0

            if output_file_path.exists():
                result_text = output_file_path.read_text(encoding="utf-8").strip()
            if not result_text:
                result_text = "\n".join(line for line in stderr_lines if line).strip()
        except asyncio.CancelledError:
            if running_job is not None and hasattr(running_job.process, "returncode"):
                if running_job.process.returncode is None:
                    running_job.process.terminate()
                    await running_job.process.wait()
            self.submit_sessions.pop(chat_id, None)
            self.active_jobs.pop(chat_id, None)
            raise
        finally:
            if output_file_path.exists():
                output_file_path.unlink()

        self.submit_sessions.pop(chat_id, None)
        completed_job = self.active_jobs.pop(chat_id, None)
        if completed_job is not None and completed_job.termination_requested:
            return
        await self.client.send_message(
            chat_id,
            self._completion_message(
                success,
                result_text,
                selected_directory,
                completed_job.usage_summary if completed_job is not None else None,
            ),
        )

    @staticmethod
    def _job_is_running(job: RunningCodexJob) -> bool:
        if hasattr(job.handle, "done"):
            return not job.handle.done()
        return True

    async def _consume_codex_stdout(
        self,
        stream: asyncio.StreamReader | None,
        running_job: RunningCodexJob | None,
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text or running_job is None:
                continue
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                running_job.latest_status = "stdout"
                running_job.latest_message = text
                continue
            apply_codex_event(running_job, event)

    async def _consume_codex_stderr(
        self,
        stream: asyncio.StreamReader | None,
        running_job: RunningCodexJob | None,
        stderr_lines: list[str],
    ) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            stderr_lines.append(text)
            if running_job is not None:
                running_job.latest_status = "stderr"
                running_job.latest_message = text


def is_git_repository(workdir: Path) -> bool:
    """Return True when the selected working directory looks like a Git repository."""
    return (workdir / ".git").exists()


def build_gateway_submit_prompt(workdir: Path, prompt: str) -> str:
    """Inject Telegram-specific completion requirements ahead of the user task."""
    resolved_workdir = workdir.expanduser().resolve()
    return (
        "Telegram gateway requirements:\n"
        f"- You are working in project directory: {resolved_workdir}\n"
        "- Before you finish, you MUST call the `telegram_feedback` tool.\n"
        f"- Use `project_directory` set to `{resolved_workdir}`.\n"
        "- Provide a concise `summary` of the work, current state, and any question for the user.\n"
        "- Do NOT use `interactive_feedback`.\n"
        "- Do NOT finish the task until `telegram_feedback` returns.\n"
        "\n"
        "User task:\n"
        f"{prompt}"
    )


def build_gateway_resume_prompt(workdir: Path, prompt: str) -> str:
    """Build the fixed prompt used for non-interactive resume jobs."""
    resolved_workdir = workdir.expanduser().resolve()
    return (
        "Continue the previously resumed task from its current state.\n"
        "Telegram gateway requirements:\n"
        f"- You are working in project directory: {resolved_workdir}\n"
        "- Before you finish, you MUST call the `telegram_feedback` tool.\n"
        f"- Use `project_directory` set to `{resolved_workdir}`.\n"
        "- Provide a concise `summary` of the work, current state, and any question for the user.\n"
        "- Do NOT use `interactive_feedback`.\n"
        "- Do NOT finish the task until `telegram_feedback` returns.\n"
        "\n"
        "Additional instruction from user:\n"
        f"{prompt}"
    )


def build_codex_exec_command(
    workdir: Path,
    prompt: str,
    output_file: Path | None = None,
) -> list[str]:
    """Build a Codex non-interactive command for the selected directory."""
    resolved_workdir = workdir.expanduser().resolve()
    command = [
        "codex",
        "exec",
        "--full-auto",
        "--json",
        "-C",
        str(resolved_workdir),
    ]
    if output_file is not None:
        command.extend(["-o", str(output_file.expanduser().resolve())])
    if not is_git_repository(resolved_workdir):
        command.append("--skip-git-repo-check")
    command.append(prompt)
    return command


def build_codex_resume_command(
    workdir: Path,
    prompt: str,
    output_file: Path | None = None,
) -> list[str]:
    """Build a Codex resume command for the selected directory."""
    resolved_workdir = workdir.expanduser().resolve()
    command = [
        "codex",
        "exec",
        "resume",
        "--last",
        "--full-auto",
        "--json",
    ]
    if output_file is not None:
        command.extend(["-o", str(output_file.expanduser().resolve())])
    if not is_git_repository(resolved_workdir):
        command.append("--skip-git-repo-check")
    command.append(prompt)
    return command


def extract_agent_message_text(message: dict) -> str:
    """Extract plain text from a Codex agent_message event payload."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def format_usage_summary(usage: dict | None) -> str | None:
    """Render a compact token usage summary."""
    if not isinstance(usage, dict):
        return None
    fields = [
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("reasoning_tokens", "reasoning"),
    ]
    parts = []
    for key, label in fields:
        value = usage.get(key)
        if isinstance(value, int):
            parts.append(f"{label} {value}")
    if not parts:
        return None
    return " / ".join(parts)


def apply_codex_event(job: RunningCodexJob, event: dict) -> None:
    """Update the tracked task state from one Codex JSON event."""
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type != "turn.completed":
        job.latest_status = event_type

    if event_type == "agent_message":
        message = event.get("message")
        if isinstance(message, dict):
            text = extract_agent_message_text(message)
            if text:
                job.latest_message = text

    usage = event.get("usage")
    if not isinstance(usage, dict):
        turn = event.get("turn")
        if isinstance(turn, dict):
            usage = turn.get("usage")
    usage_summary = format_usage_summary(usage if isinstance(usage, dict) else None)
    if usage_summary:
        job.usage_summary = usage_summary


async def feedback_server_seems_configured() -> bool:
    """Best-effort preflight for Codex MCP config that should expose telegram_feedback."""
    process = await asyncio.create_subprocess_exec(
        "codex",
        "mcp",
        "list",
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return False
    try:
        servers = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return False
    if not isinstance(servers, list):
        return False

    for server in servers:
        if not isinstance(server, dict) or not server.get("enabled", False):
            continue
        name = server.get("name")
        if isinstance(name, str) and name in {"feedback", "mcp-feedback-enhanced"}:
            return True
        transport = server.get("transport")
        if not isinstance(transport, dict):
            continue
        command = transport.get("command")
        args = transport.get("args")
        url = transport.get("url")
        haystacks: list[str] = []
        if isinstance(command, str):
            haystacks.append(command)
        if isinstance(args, list):
            haystacks.extend(item for item in args if isinstance(item, str))
        if isinstance(url, str):
            haystacks.append(url)
        if any("mcp-feedback-enhanced" in item for item in haystacks):
            return True

    return False


async def ensure_gateway_commands(client: TelegramBotClient) -> None:
    """Ensure gateway mode command set includes /submit."""
    current_commands = await client.get_commands()
    merged_commands: list[dict[str, str]] = []
    command_index: dict[str, dict[str, str]] = {}

    for command in current_commands:
        if not isinstance(command, dict):
            continue
        name = command.get("command")
        if not isinstance(name, str) or not name:
            continue
        description = command.get("description")
        normalized = {
            "command": name,
            "description": description if isinstance(description, str) else "",
        }
        merged_commands.append(normalized)
        command_index[name] = normalized

    needs_update = False
    for required_command in GATEWAY_TELEGRAM_COMMANDS:
        existing = command_index.get(required_command["command"])
        if existing is None:
            merged_commands.append(dict(required_command))
            needs_update = True
        elif existing["description"] != required_command["description"]:
            existing["description"] = required_command["description"]
            needs_update = True

    if needs_update:
        await client.set_commands(merged_commands)


def load_gateway_config() -> dict[str, str | Path]:
    """Load Telegram gateway configuration from environment variables."""
    bot_token = os.getenv("MCP_TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise ValueError("Missing required environment variable: MCP_TELEGRAM_BOT_TOKEN")

    chat_id = os.getenv("MCP_TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        raise ValueError("Missing required environment variable: MCP_TELEGRAM_CHAT_ID")

    api_base = os.getenv("MCP_TELEGRAM_API_BASE", DEFAULT_TELEGRAM_API_BASE).strip()
    if not api_base:
        api_base = DEFAULT_TELEGRAM_API_BASE

    root = Path(os.getenv("MCP_CODEX_ROOT", str(DEFAULT_DIRECTORY_ROOT))).expanduser()
    return {
        "bot_token": bot_token,
        "chat_id": chat_id,
        "api_base": api_base.rstrip("/"),
        "root": root,
    }


async def run_gateway_loop() -> None:
    """Run the Telegram gateway long-polling loop."""
    config = load_gateway_config()
    client = TelegramBotClient(
        token=str(config["bot_token"]),
        api_base=str(config["api_base"]),
    )
    await ensure_gateway_commands(client)
    service = TelegramGatewayService(
        client=client,
        root=Path(config["root"]),
    )

    updates = await client.get_updates(timeout=0)
    next_offset = 0
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset, update_id)
    if next_offset:
        next_offset += 1

    target_chat_id = str(config["chat_id"])

    while True:
        updates = await client.get_updates(offset=next_offset or None, timeout=30)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1

            callback_query = update.get("callback_query")
            if isinstance(callback_query, dict):
                message = callback_query.get("message") or {}
                chat = message.get("chat") or {}
                if str(chat.get("id")) == target_chat_id:
                    await service.handle_callback_query(callback_query)
                continue

            message = update.get("message")
            if isinstance(message, dict):
                chat = message.get("chat") or {}
                if str(chat.get("id")) == target_chat_id:
                    await service.handle_message(message)
