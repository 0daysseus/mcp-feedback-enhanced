#!/usr/bin/env python3
"""
Telegram gateway helpers for directory browsing and Codex execution.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from math import ceil
from pathlib import Path

from ..debug import debug_log
from ..feedback_routing import is_user_away, set_user_away
from . import codex_app_server
from .client import TelegramBotClient, TelegramClientError
from .completion_confirmation import resolve_completion_confirmation
from .pending_feedback import handle_pending_feedback_message
from .session import DEFAULT_TELEGRAM_API_BASE


DEFAULT_DIRECTORY_ROOT = Path("/home/kube")
TELEGRAM_MESSAGE_TEXT_LIMIT = 3500
SAVED_SESSIONS_LIST_PAGE_SIZE = 5
COMPLETION_CONFIRMATION_TOOL_NAME = "telegram_confirm_completion"
BASE_GATEWAY_TELEGRAM_COMMANDS = [
    {"command": "done", "description": "Submit feedback"},
    {"command": "steer", "description": "Send follow-up input to a loaded session"},
    {"command": "submit", "description": "Run Codex in a selected directory"},
    {"command": "resume", "description": "Continue a saved Codex session"},
    {"command": "sessions", "description": "List saved Codex sessions"},
    {"command": "tasks", "description": "List loaded Codex sessions"},
]
MANAGED_GATEWAY_COMMAND_NAMES = {
    "done",
    "cancel",
    "away",
    "steer",
    "submit",
    "resume",
    "sessions",
    "tasks",
}


def build_gateway_commands() -> list[dict[str, str]]:
    """Build the gateway slash command list with state-aware away description."""
    away_description = (
        "Mark yourself back at the computer"
        if is_user_away()
        else "Mark yourself away from the computer"
    )
    return [
        BASE_GATEWAY_TELEGRAM_COMMANDS[0],
        {"command": "away", "description": away_description},
        *BASE_GATEWAY_TELEGRAM_COMMANDS[1:],
    ]


def gateway_debug_log(message: str) -> None:
    """Write Telegram gateway debug logs without touching stdout."""
    debug_log(message, "TG-GW")


def paginate_saved_sessions(
    sessions: tuple[codex_app_server.CodexThreadSummary, ...],
    page: int,
    page_size: int,
) -> tuple[tuple[codex_app_server.CodexThreadSummary, ...], int, int]:
    """Return one page of saved sessions plus safe page metadata."""
    total_pages = max(1, ceil(len(sessions) / page_size))
    safe_page = min(max(page, 0), total_pages - 1)
    start = safe_page * page_size
    end = start + page_size
    return sessions[start:end], total_pages, safe_page


def _truncate_middle(text: str, max_length: int) -> str:
    """Trim long display values so Telegram pages stay readable."""
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    head = max_length // 2 - 1
    tail = max_length - head - 3
    return f"{text[:head]}...{text[-tail:]}"


def split_telegram_text(text: str, max_length: int = TELEGRAM_MESSAGE_TEXT_LIMIT) -> list[str]:
    """Split long Telegram text into safe chunks, preferring line boundaries."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_length:
            if current:
                chunks.append(current.rstrip())
                current = ""
            for start in range(0, len(line), max_length):
                chunks.append(line[start : start + max_length].rstrip())
            continue

        if current and len(current) + len(line) > max_length:
            chunks.append(current.rstrip())
            current = line
            continue

        current = f"{current}{line}"

    if current:
        chunks.append(current.rstrip())

    return chunks or [text[:max_length]]


async def poll_updates_with_retry(
    client: TelegramBotClient,
    *,
    offset: int | None = None,
    timeout: int = 30,
    retry_delay: float = 5.0,
) -> list[dict]:
    """Retry Telegram long-poll requests when the upstream temporarily fails."""
    while True:
        try:
            return await client.get_updates(offset=offset, timeout=timeout)
        except (TelegramClientError, TimeoutError) as exc:
            gateway_debug_log(
                "Telegram getUpdates failed; retrying "
                f"offset={offset} timeout={timeout} error={type(exc).__name__}: {exc}"
            )
            await asyncio.sleep(retry_delay)


def detect_completion_confirmation_state(thread_payload: dict[str, object]) -> str:
    """Return whether the latest completed turn recorded an approval decision."""
    thread = thread_payload.get("thread")
    if not isinstance(thread, dict):
        return "missing"
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return "missing"
    last_turn = turns[-1]
    if not isinstance(last_turn, dict):
        return "missing"
    if last_turn.get("status") != "completed":
        return "missing"

    items = last_turn.get("items")
    if not isinstance(items, list):
        return "missing"

    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "mcpToolCall":
            continue
        if item.get("tool") != COMPLETION_CONFIRMATION_TOOL_NAME:
            continue
        result = item.get("result")
        if isinstance(result, dict) and result.get("approved") is True:
            return "approved"
        return "rejected"

    return "missing"


def build_missing_completion_confirmation_prompt() -> str:
    """Prompt Codex to explicitly ask the Telegram user for completion approval."""
    return (
        "The previous turn must not finish yet.\n"
        f"It ended without calling `{COMPLETION_CONFIRMATION_TOOL_NAME}`.\n"
        "Away mode requires explicit Telegram approval before the agent may stop.\n"
        f"Call `{COMPLETION_CONFIRMATION_TOOL_NAME}` now with a concise summary of the completed work.\n"
        "If the user does not approve, use `telegram_feedback` to ask for the next instruction and keep working."
    )


def build_rejected_completion_confirmation_prompt() -> str:
    """Prompt Codex to continue because the Telegram user rejected completion."""
    return (
        "The Telegram user rejected task completion in the previous turn.\n"
        "You must continue instead of stopping.\n"
        "Use `telegram_feedback` now to ask what should happen next, then continue the task.\n"
        f"Do not finish another turn until `{COMPLETION_CONFIRMATION_TOOL_NAME}` returns approval."
    )


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
    available_sessions: tuple[codex_app_server.CodexThreadSummary, ...] = ()
    selected_thread_id: str | None = None
    selected_session_title: str | None = None
    draft_prompt: str = ""


@dataclass(slots=True)
class CodexLaunchRequest:
    """Normalized launch request for submit or resume actions."""

    chat_id: str
    requested_action: str
    selected_directory: Path
    prompt: str
    thread_id: str | None = None
    session_title: str | None = None


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
    thread_id: str | None = None
    turn_id: str | None = None
    session_title: str | None = None
    pending_user_input: "PendingUserInputState | None" = None


@dataclass(slots=True)
class PendingUserInputState:
    """One in-flight request_user_input interaction for a running Codex job."""

    request_id: int | str
    turn_id: str | None
    item_id: str | None
    questions: tuple[codex_app_server.RequestUserInputQuestion, ...]
    response_future: asyncio.Future[dict[str, object]]
    current_index: int = 0
    message_id: int | None = None
    answers: dict[str, list[str]] = field(default_factory=dict)


def run_gateway() -> None:
    """Run the Telegram gateway event loop."""
    gateway_debug_log("Starting Telegram gateway event loop")
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
        launch_codex_job: Callable[[CodexLaunchRequest], Awaitable[None]] | None = None,
        feedback_mcp_available: Callable[[], Awaitable[bool]] | None = None,
        app_server_factory: Callable[[], codex_app_server.CodexAppServer] | None = None,
        list_codex_threads: Callable[
            [], Awaitable[list[codex_app_server.CodexThreadSummary]]
        ]
        | None = None,
    ):
        self.client = client
        self.root = resolve_directory(root, root)
        self.page_size = page_size
        self.submit_sessions: dict[str, SubmitSessionState] = {}
        self.active_jobs: dict[str, RunningCodexJob] = {}
        self.loaded_sessions: dict[str, list[RunningCodexJob]] = {}
        self._launch_codex_job = launch_codex_job or self._launch_codex_job_impl
        self._feedback_mcp_available = (
            feedback_mcp_available or feedback_server_seems_configured
        )
        self._app_server_factory = app_server_factory or codex_app_server.CodexAppServer
        self._list_codex_threads = list_codex_threads or self._list_codex_threads_impl

    async def handle_message(self, message: dict) -> None:
        """Handle a Telegram message update."""
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id"))
        try:
            await self._handle_message_impl(message)
        except Exception as exc:
            gateway_debug_log(
                f"Error while handling Telegram message chat={chat_id}: {type(exc).__name__}: {exc}"
            )
            await self._send_gateway_error(chat_id, exc)

    async def _handle_message_impl(self, message: dict) -> None:
        """Core Telegram message handler."""
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id"))
        text = message.get("text")

        if isinstance(text, str) and text.startswith("/"):
            gateway_debug_log(f"Received Telegram command chat={chat_id} text={text}")

        if await handle_pending_feedback_message(message, self.client):
            return

        if not isinstance(text, str):
            return

        state = self.submit_sessions.get(chat_id)
        if state is not None and state.mode == "collecting_steer_prompt":
            await self._handle_collecting_steer_prompt(state, text)
            return

        if text.startswith("/away"):
            await self._handle_away_command(chat_id, text)
            return

        if text == "/steer":
            await self._start_steer_flow(chat_id)
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
            await self._start_saved_session_browser(chat_id)
            return

        if text == "/sessions":
            await self._show_saved_sessions(chat_id)
            return

        if text in {"/tasks", "/task"}:
            await self._show_loaded_sessions(chat_id)
            return

        running_job = self.active_jobs.get(chat_id)
        if (
            running_job is not None
            and self._job_is_running(running_job)
            and not text.startswith("/")
        ):
            if isinstance(running_job.pending_user_input, PendingUserInputState):
                await self.client.send_message(
                    chat_id,
                    "Codex is waiting for structured input. Please answer using the inline buttons.",
                )
                return
            await self._steer_running_job(running_job, text)
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
            await self._launch_codex_job(self._build_launch_request(state, text))
            self.submit_sessions[chat_id] = replace(state, mode="running_codex_job")
            if state.requested_action == "resume":
                await self.client.send_message(chat_id, "Resume started. Codex is working.")
            else:
                await self.client.send_message(chat_id, "Task started. Codex is working.")

    async def _handle_away_command(self, chat_id: str, text: str) -> None:
        """Show or update the persisted away-mode flag used by feedback routing."""
        parts = text.split(maxsplit=1)
        current_away = is_user_away()
        current_status = "ON" if current_away else "OFF"

        if len(parts) == 1:
            next_away = not current_away
            set_user_away(next_away)
            await ensure_gateway_commands(self.client)
            gateway_debug_log(
                f"Toggled away mode via /away chat={chat_id} from={current_status} to={'ON' if next_away else 'OFF'}"
            )
            await self.client.send_message(
                chat_id,
                (
                    "Away mode enabled.\n"
                    "Agents should now use `telegram_feedback` instead of `interactive_feedback`."
                )
                if next_away
                else (
                    "Away mode disabled.\n"
                    "Non-Telegram tasks should use `interactive_feedback` first.\n"
                    "Telegram-started tasks must still use `telegram_feedback`."
                ),
            )
            return

        desired_state = parts[1].strip().lower()
        if desired_state == "on":
            set_user_away(True)
            await ensure_gateway_commands(self.client)
            gateway_debug_log(f"Set away mode via /away on chat={chat_id}")
            await self.client.send_message(
                chat_id,
                (
                    "Away mode enabled.\n"
                    "Agents should now use `telegram_feedback` instead of `interactive_feedback`."
                ),
            )
            return

        if desired_state == "off":
            set_user_away(False)
            await ensure_gateway_commands(self.client)
            gateway_debug_log(f"Set away mode via /away off chat={chat_id}")
            await self.client.send_message(
                chat_id,
                (
                    "Away mode disabled.\n"
                    "Non-Telegram tasks should use `interactive_feedback` first.\n"
                    "Telegram-started tasks must still use `telegram_feedback`."
                ),
            )
            return

        await self.client.send_message(
            chat_id,
            (
                "Usage: `/away on` or `/away off`.\n"
                f"Current away mode: {current_status}."
            ),
        )

    async def _start_steer_flow(self, chat_id: str) -> None:
        """Begin collecting a steer prompt from Telegram text."""
        if not self._loaded_sessions_for_chat(chat_id):
            await self._send_message(
                chat_id,
                "No loaded sessions are available to steer.",
            )
            return

        self.submit_sessions[chat_id] = SubmitSessionState(
            chat_id=chat_id,
            mode="collecting_steer_prompt",
            requested_action="steer",
            root_directory=self.root,
            current_directory=self.root,
        )
        await self._send_message(
            chat_id,
            "Send the steer text.\n"
            "You can send multiple plain-text messages.\n"
            "When you are done, send `/done` to choose a loaded session.",
        )

    async def _handle_collecting_steer_prompt(
        self,
        state: SubmitSessionState,
        text: str,
    ) -> None:
        """Collect steer text until the user sends /done."""
        chat_id = state.chat_id
        if text == "/cancel":
            self.submit_sessions.pop(chat_id, None)
            await self._send_message(chat_id, "Telegram steer flow cancelled.")
            return

        if text == "/done":
            prompt = state.draft_prompt.strip()
            if not prompt:
                await self._send_message(
                    chat_id,
                    "Send at least one steer message before `/done`.",
                )
                return
            await self._show_loaded_session_picker_for_steer(chat_id, prompt)
            return

        if text.startswith("/"):
            await self._send_message(
                chat_id,
                "While collecting steer text, send plain text or `/done`.",
            )
            return

        next_prompt = f"{state.draft_prompt}\n{text}".strip() if state.draft_prompt else text
        self.submit_sessions[chat_id] = replace(state, draft_prompt=next_prompt)
        await self._send_message(
            chat_id,
            "Steer text recorded. Send more text or `/done`.",
        )

    async def _show_loaded_session_picker_for_steer(
        self,
        chat_id: str,
        prompt: str,
    ) -> None:
        """Prompt the user to choose a loaded session for the steer text."""
        available_sessions = self._loaded_session_summaries(chat_id)
        if not available_sessions:
            self.submit_sessions.pop(chat_id, None)
            await self._send_message(
                chat_id,
                "No loaded sessions are available to steer.",
            )
            return

        updated_state = SubmitSessionState(
            chat_id=chat_id,
            mode="browsing_loaded_sessions_for_steer",
            requested_action="steer",
            root_directory=self.root,
            current_directory=self.root,
            available_sessions=available_sessions,
            draft_prompt=prompt,
        )
        self.submit_sessions[chat_id] = updated_state
        await self._send_message(
            chat_id,
            self._loaded_sessions_list_text(
                available_sessions,
                title="Select a loaded session to steer",
                page=0,
            ),
            reply_markup=self._loaded_sessions_list_keyboard(
                available_sessions,
                page=0,
                callback_prefix="steer",
            ),
        )

    async def _send_gateway_error(self, chat_id: str, error: Exception) -> None:
        """Send a compact Telegram-visible error message without crashing the loop."""
        if not chat_id or chat_id == "None":
            return

        error_text = _truncate_middle(
            f"{type(error).__name__}: {error}",
            800,
        )
        try:
            await self.client.send_message(
                chat_id,
                "Telegram gateway error:\n"
                f"{error_text}",
            )
        except Exception as notify_exc:
            gateway_debug_log(
                "Failed to send gateway error message "
                f"chat={chat_id}: {type(notify_exc).__name__}: {notify_exc}"
            )

    async def _send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict[str, list[list[dict[str, str]]]] | None = None,
    ) -> dict:
        """Send Telegram text safely, splitting long plain-text payloads when needed."""
        if reply_markup is not None:
            safe_text = text
            if len(safe_text) > TELEGRAM_MESSAGE_TEXT_LIMIT:
                gateway_debug_log(
                    f"Truncating reply-markup message chat={chat_id} len={len(safe_text)}"
                )
                safe_text = _truncate_middle(safe_text, TELEGRAM_MESSAGE_TEXT_LIMIT)
            return await self.client.send_message(
                chat_id,
                safe_text,
                reply_markup=reply_markup,
            )

        chunks = split_telegram_text(text, TELEGRAM_MESSAGE_TEXT_LIMIT)
        result: dict | None = None
        if len(chunks) > 1:
            gateway_debug_log(
                f"Splitting long Telegram message chat={chat_id} into {len(chunks)} chunks"
            )
        for chunk in chunks:
            result = await self.client.send_message(chat_id, chunk)
        return result or {"message_id": None}

    async def _edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict[str, list[list[dict[str, str]]]] | None = None,
    ) -> bool:
        """Edit Telegram text safely by trimming oversized payloads."""
        safe_text = text
        if len(safe_text) > TELEGRAM_MESSAGE_TEXT_LIMIT:
            gateway_debug_log(
                f"Truncating edited Telegram message chat={chat_id} len={len(safe_text)}"
            )
            safe_text = _truncate_middle(safe_text, TELEGRAM_MESSAGE_TEXT_LIMIT)
        return await self.client.edit_message_text(
            chat_id,
            message_id,
            safe_text,
            reply_markup=reply_markup,
        )

    async def handle_callback_query(self, callback_query: dict) -> None:
        """Handle a Telegram callback query from inline buttons."""
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id"))
        try:
            await self._handle_callback_query_impl(callback_query)
        except Exception as exc:
            gateway_debug_log(
                f"Error while handling callback query chat={chat_id}: {type(exc).__name__}: {exc}"
            )
            await self._send_gateway_error(chat_id, exc)

    async def _handle_callback_query_impl(self, callback_query: dict) -> None:
        """Core callback-query handler."""
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
                await self._edit_message_text(
                    chat_id,
                    message["message_id"],
                    "No running tasks.",
                    reply_markup=None,
                )
            else:
                await self._edit_message_text(
                    chat_id,
                    message["message_id"],
                    self._task_status_text(running_job),
                    reply_markup=self._task_keyboard(),
                )
            await self.client.answer_callback_query(callback_query.get("id"), text=None)
            return

        if data.startswith("tcc:approve:") or data.startswith("tcc:reject:"):
            approved = data.startswith("tcc:approve:")
            request_id = data.rsplit(":", 1)[-1]
            resolved_request = resolve_completion_confirmation(
                request_id,
                approved=approved,
            )
            if resolved_request is None:
                await self.client.answer_callback_query(
                    callback_query.get("id"),
                    text="This completion check is no longer active.",
                )
                return
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                (
                    "Task completion approved. The agent may stop now."
                    if approved
                    else "Task completion rejected. The agent must continue."
                ),
                reply_markup=None,
            )
            await self.client.answer_callback_query(callback_query.get("id"), text=None)
            return

        if data.startswith("rui:pick:"):
            running_job = self.active_jobs.get(chat_id)
            pending_request = (
                running_job.pending_user_input
                if running_job is not None
                and isinstance(running_job.pending_user_input, PendingUserInputState)
                else None
            )
            if running_job is None or pending_request is None:
                await self.client.answer_callback_query(
                    callback_query.get("id"),
                    text="This question is no longer active.",
                )
                return
            option_index = int(data.rsplit(":", 1)[-1])
            try:
                await self._record_user_input_answer(
                    running_job,
                    pending_request,
                    chat_id,
                    message["message_id"],
                    option_index,
                )
            except ValueError:
                await self.client.answer_callback_query(
                    callback_query.get("id"),
                    text="This option is no longer available.",
                )
                return
            await self.client.answer_callback_query(callback_query.get("id"), text=None)
            return

        state = self.submit_sessions.get(chat_id)
        if state is None:
            await self.client.answer_callback_query(callback_query.get("id"), text=None)
            return

        if data.startswith("tasks:list:page:"):
            loaded_sessions = self._loaded_session_summaries(chat_id)
            current_page = state.page if state.mode == "listing_loaded_sessions" else 0
            delta = int(data.rsplit(":", 1)[-1])
            next_page = max(current_page + delta, 0)
            self.submit_sessions[chat_id] = SubmitSessionState(
                chat_id=chat_id,
                mode="listing_loaded_sessions",
                requested_action="tasks",
                root_directory=self.root,
                current_directory=self.root,
                available_sessions=loaded_sessions,
                page=next_page,
            )
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                self._loaded_sessions_list_text(
                    loaded_sessions,
                    title="Loaded Codex sessions",
                    page=next_page,
                ),
                reply_markup=self._loaded_sessions_list_keyboard(
                    loaded_sessions,
                    page=next_page,
                    callback_prefix="tasks",
                ),
            )
        elif data == "tasks:list:close":
            self.submit_sessions.pop(chat_id, None)
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                "Loaded session list closed.",
                reply_markup=None,
            )
        elif data.startswith("steer:list:page:"):
            delta = int(data.rsplit(":", 1)[-1])
            updated_state = replace(state, page=max(state.page + delta, 0))
            self.submit_sessions[chat_id] = updated_state
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                self._loaded_sessions_list_text(
                    updated_state.available_sessions,
                    title="Select a loaded session to steer",
                    page=updated_state.page,
                ),
                reply_markup=self._loaded_sessions_list_keyboard(
                    updated_state.available_sessions,
                    page=updated_state.page,
                    callback_prefix="steer",
                ),
            )
        elif data == "steer:list:close":
            self.submit_sessions.pop(chat_id, None)
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                "Telegram steer flow cancelled.",
                reply_markup=None,
            )
        elif data.startswith("steer:pick:"):
            index = int(data.rsplit(":", 1)[-1])
            if index < 0 or index >= len(state.available_sessions):
                await self.client.answer_callback_query(
                    callback_query.get("id"),
                    text="This loaded-session list is stale. Please try again.",
                )
                return
            selected_session = state.available_sessions[index]
            loaded_job = self._find_loaded_session(chat_id, selected_session.thread_id)
            if loaded_job is None:
                await self.client.answer_callback_query(
                    callback_query.get("id"),
                    text="This loaded session is no longer available.",
                )
                return
            self.submit_sessions.pop(chat_id, None)
            await self._dispatch_steer_to_loaded_session(
                loaded_job,
                state.draft_prompt,
            )
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                (
                    "Steer dispatched to loaded session:\n"
                    f"{selected_session.display_name}"
                ),
                reply_markup=None,
            )
        elif data.startswith("ses:list:page:"):
            delta = int(data.rsplit(":", 1)[-1])
            updated_state = replace(state, page=max(state.page + delta, 0))
            self.submit_sessions[chat_id] = updated_state
            gateway_debug_log(
                f"Paginating /sessions chat={chat_id} to page={updated_state.page + 1}"
            )
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                self._saved_sessions_list_text(
                    updated_state.available_sessions,
                    page=updated_state.page,
                ),
                reply_markup=self._saved_sessions_list_keyboard(
                    updated_state.available_sessions,
                    page=updated_state.page,
                ),
            )
        elif data == "ses:list:close":
            self.submit_sessions.pop(chat_id, None)
            await self._edit_message_text(
                chat_id,
                message["message_id"],
                "Saved session list closed.",
                reply_markup=None,
            )
        elif data.startswith("ses:pick:"):
            index = int(data.rsplit(":", 1)[-1])
            if index < 0 or index >= len(state.available_sessions):
                await self.client.answer_callback_query(
                    callback_query.get("id"),
                    text="This session list is stale. Please try again.",
                )
                return
            selected_session = state.available_sessions[index]
            updated_state = replace(
                state,
                mode="waiting_for_task_prompt",
                selected_directory=selected_session.cwd,
                selected_thread_id=selected_session.thread_id,
                selected_session_title=selected_session.display_name,
            )
            self.submit_sessions[chat_id] = updated_state
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                (
                    "Send the resume prompt for:\n"
                    f"{selected_session.display_name}\n"
                    f"{selected_session.cwd}"
                ),
                reply_markup=None,
            )
        elif data == "ses:cancel":
            self.submit_sessions.pop(chat_id, None)
            await self.client.edit_message_text(
                chat_id,
                message["message_id"],
                "Telegram resume flow cancelled.",
                reply_markup=None,
            )
        elif data.startswith("sub:open:"):
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
        result = await self._send_message(
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

    async def _start_saved_session_browser(self, chat_id: str) -> None:
        saved_sessions = tuple(await self._list_codex_threads())
        if not saved_sessions:
            await self._send_message(
                chat_id,
                "No resumable Codex sessions were found under the allowed root.",
            )
            return

        state = SubmitSessionState(
            chat_id=chat_id,
            mode="browsing_saved_sessions",
            requested_action="resume",
            root_directory=self.root,
            current_directory=self.root,
            available_sessions=saved_sessions,
        )
        result = await self._send_message(
            chat_id,
            self._saved_sessions_text(saved_sessions),
            reply_markup=self._saved_sessions_keyboard(saved_sessions),
        )
        self.submit_sessions[chat_id] = replace(
            state,
            browser_message_id=result.get("message_id"),
        )

    def _loaded_sessions_for_chat(self, chat_id: str) -> list[RunningCodexJob]:
        """Return loaded sessions known for one Telegram chat."""
        sessions = list(self.loaded_sessions.get(chat_id, []))
        active_job = self.active_jobs.get(chat_id)
        if active_job is not None and active_job.thread_id:
            sessions = [
                existing
                for existing in sessions
                if existing.thread_id != active_job.thread_id
            ]
            sessions.insert(0, active_job)
        return sessions

    def _find_loaded_session(
        self,
        chat_id: str,
        thread_id: str | None,
    ) -> RunningCodexJob | None:
        """Find one loaded session by thread id."""
        if not thread_id:
            return None
        for job in self._loaded_sessions_for_chat(chat_id):
            if job.thread_id == thread_id:
                return job
        return None

    def _loaded_session_summaries(
        self,
        chat_id: str,
    ) -> tuple[codex_app_server.CodexThreadSummary, ...]:
        """Render loaded-session records as thread summaries for Telegram UI."""
        jobs = sorted(
            self._loaded_sessions_for_chat(chat_id),
            key=lambda job: (
                0 if job.latest_status == "active" else 1,
                job.session_title or job.prompt or job.thread_id or "",
            ),
        )
        return tuple(
            codex_app_server.CodexThreadSummary(
                thread_id=job.thread_id or f"loaded-{index}",
                cwd=job.selected_directory,
                preview=job.session_title or job.prompt or "Loaded session",
                name=job.session_title,
                updated_at=None,
                status_type=job.latest_status,
            )
            for index, job in enumerate(jobs)
        )

    async def _show_loaded_sessions(self, chat_id: str) -> None:
        loaded_sessions = self._loaded_session_summaries(chat_id)
        if not loaded_sessions:
            await self._send_message(chat_id, "No loaded Codex sessions.")
            return

        self.submit_sessions[chat_id] = SubmitSessionState(
            chat_id=chat_id,
            mode="listing_loaded_sessions",
            requested_action="tasks",
            root_directory=self.root,
            current_directory=self.root,
            available_sessions=loaded_sessions,
            page=0,
        )

        await self._send_message(
            chat_id,
            self._loaded_sessions_list_text(
                loaded_sessions,
                title="Loaded Codex sessions",
                page=0,
            ),
            reply_markup=self._loaded_sessions_list_keyboard(
                loaded_sessions,
                page=0,
                callback_prefix="tasks",
            ),
        )

    async def _show_saved_sessions(self, chat_id: str) -> None:
        saved_sessions = tuple(await self._list_codex_threads())
        if not saved_sessions:
            gateway_debug_log(f"/sessions returned no resumable sessions for chat={chat_id}")
            await self._send_message(
                chat_id,
                "No resumable Codex sessions were found under the allowed root.",
            )
            return
        gateway_debug_log(
            f"/sessions returned {len(saved_sessions)} resumable sessions for chat={chat_id}"
        )
        state = SubmitSessionState(
            chat_id=chat_id,
            mode="listing_saved_sessions",
            requested_action="sessions",
            root_directory=self.root,
            current_directory=self.root,
            available_sessions=saved_sessions,
            page=0,
        )
        response = await self._send_message(
            chat_id,
            self._saved_sessions_list_text(saved_sessions, page=0),
            reply_markup=self._saved_sessions_list_keyboard(saved_sessions, page=0),
        )
        self.submit_sessions[chat_id] = replace(
            state,
            browser_message_id=response.get("message_id"),
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
    def _saved_sessions_keyboard(
        sessions: tuple[codex_app_server.CodexThreadSummary, ...],
    ) -> dict[str, list[list[dict[str, str]]]]:
        inline_keyboard = [
            [
                {
                    "text": TelegramGatewayService._saved_session_button_text(index, session),
                    "callback_data": f"ses:pick:{index}",
                }
            ]
            for index, session in enumerate(sessions)
        ]
        inline_keyboard.append([{"text": "Cancel", "callback_data": "ses:cancel"}])
        return {"inline_keyboard": inline_keyboard}

    @staticmethod
    def _saved_sessions_list_keyboard(
        sessions: tuple[codex_app_server.CodexThreadSummary, ...],
        page: int,
    ) -> dict[str, list[list[dict[str, str]]]]:
        _, total_pages, safe_page = paginate_saved_sessions(
            sessions,
            page,
            SAVED_SESSIONS_LIST_PAGE_SIZE,
        )
        inline_keyboard: list[list[dict[str, str]]] = []
        navigation_row: list[dict[str, str]] = []
        if safe_page > 0:
            navigation_row.append(
                {"text": "Previous page", "callback_data": "ses:list:page:-1"}
            )
        if safe_page < total_pages - 1:
            navigation_row.append(
                {"text": "Next page", "callback_data": "ses:list:page:+1"}
            )
        if navigation_row:
            inline_keyboard.append(navigation_row)
        inline_keyboard.append([{"text": "Close", "callback_data": "ses:list:close"}])
        return {"inline_keyboard": inline_keyboard}

    @staticmethod
    def _loaded_sessions_list_keyboard(
        sessions: tuple[codex_app_server.CodexThreadSummary, ...],
        *,
        page: int,
        callback_prefix: str,
    ) -> dict[str, list[list[dict[str, str]]]]:
        page_sessions, total_pages, safe_page = paginate_saved_sessions(
            sessions,
            page,
            SAVED_SESSIONS_LIST_PAGE_SIZE,
        )
        inline_keyboard: list[list[dict[str, str]]] = []

        if callback_prefix == "steer":
            start_index = safe_page * SAVED_SESSIONS_LIST_PAGE_SIZE
            for offset, session in enumerate(page_sessions):
                inline_keyboard.append(
                    [
                        {
                            "text": _truncate_middle(session.display_name, 40),
                            "callback_data": f"steer:pick:{start_index + offset}",
                        }
                    ]
                )

        navigation_row: list[dict[str, str]] = []
        if safe_page > 0:
            navigation_row.append(
                {
                    "text": "Previous page",
                    "callback_data": f"{callback_prefix}:list:page:-1",
                }
            )
        if safe_page < total_pages - 1:
            navigation_row.append(
                {
                    "text": "Next page",
                    "callback_data": f"{callback_prefix}:list:page:+1",
                }
            )
        if navigation_row:
            inline_keyboard.append(navigation_row)
        inline_keyboard.append(
            [{"text": "Close", "callback_data": f"{callback_prefix}:list:close"}]
        )
        return {"inline_keyboard": inline_keyboard}

    @staticmethod
    def _saved_sessions_text(
        sessions: tuple[codex_app_server.CodexThreadSummary, ...],
    ) -> str:
        lines = ["Select a saved Codex session to continue:", ""]
        for index, session in enumerate(sessions, start=1):
            lines.append(f"{index}. {session.display_name}")
            lines.append(f"   Thread: {session.thread_id}")
            lines.append(f"   Directory: {session.cwd}")
            if session.status_type:
                lines.append(f"   Status: {session.status_type}")
        return "\n".join(lines)

    @staticmethod
    def _saved_sessions_list_text(
        sessions: tuple[codex_app_server.CodexThreadSummary, ...],
        page: int,
    ) -> str:
        page_sessions, total_pages, safe_page = paginate_saved_sessions(
            sessions,
            page,
            SAVED_SESSIONS_LIST_PAGE_SIZE,
        )
        start_index = safe_page * SAVED_SESSIONS_LIST_PAGE_SIZE
        lines = [f"Saved Codex sessions (Page {safe_page + 1}/{total_pages}):", ""]
        for offset, session in enumerate(page_sessions, start=1):
            lines.append(
                f"{start_index + offset}. {_truncate_middle(session.display_name, 80)}"
            )
            lines.append(f"   Thread: {_truncate_middle(session.thread_id, 60)}")
            lines.append(f"   Directory: {_truncate_middle(str(session.cwd), 120)}")
            if session.status_type:
                lines.append(f"   Status: {session.status_type}")
            lines.append("")
        return _truncate_middle(
            "\n".join(lines).rstrip(),
            TELEGRAM_MESSAGE_TEXT_LIMIT,
        )

    @staticmethod
    def _loaded_sessions_list_text(
        sessions: tuple[codex_app_server.CodexThreadSummary, ...],
        *,
        title: str,
        page: int,
    ) -> str:
        page_sessions, total_pages, safe_page = paginate_saved_sessions(
            sessions,
            page,
            SAVED_SESSIONS_LIST_PAGE_SIZE,
        )
        start_index = safe_page * SAVED_SESSIONS_LIST_PAGE_SIZE
        lines = [f"{title} (Page {safe_page + 1}/{total_pages}):", ""]
        for offset, session in enumerate(page_sessions, start=1):
            lines.append(
                f"{start_index + offset}. {_truncate_middle(session.display_name, 80)}"
            )
            lines.append(f"   Thread: {_truncate_middle(session.thread_id, 60)}")
            lines.append(f"   Directory: {_truncate_middle(str(session.cwd), 120)}")
            if session.status_type:
                lines.append(f"   Status: {session.status_type}")
            lines.append("")
        return _truncate_middle("\n".join(lines).rstrip(), TELEGRAM_MESSAGE_TEXT_LIMIT)

    @staticmethod
    def _saved_session_button_text(
        index: int,
        session: codex_app_server.CodexThreadSummary,
    ) -> str:
        return f"{index + 1}. {session.display_name[:40]}"

    @staticmethod
    def _request_user_input_text(pending_request: PendingUserInputState) -> str:
        question = pending_request.questions[pending_request.current_index]
        lines = [
            (
                "Codex is waiting for user input "
                f"({pending_request.current_index + 1}/{len(pending_request.questions)}):"
            ),
            "",
        ]
        if question.header:
            lines.append(question.header)
            lines.append("")
        lines.append(question.question)
        lines.append("")
        for index, option in enumerate(question.options, start=1):
            lines.append(f"{index}. {option.label}")
            if option.description:
                lines.append(f"   {option.description}")
        return "\n".join(lines)

    @staticmethod
    def _request_user_input_keyboard(
        pending_request: PendingUserInputState,
    ) -> dict[str, list[list[dict[str, str]]]]:
        question = pending_request.questions[pending_request.current_index]
        return {
            "inline_keyboard": [
                [{"text": option.label[:60], "callback_data": f"rui:pick:{index}"}]
                for index, option in enumerate(question.options)
            ]
        }

    @staticmethod
    def _task_status_text(job: RunningCodexJob) -> str:
        title = "Running tasks:"
        task_name = job.prompt if job.prompt is not None else "Resume saved session"
        lines = [
            title,
            "",
            f"Task: {task_name}",
            f"Mode: {job.requested_action}",
            f"Directory: {job.selected_directory}",
            f"Status: {job.latest_status}",
        ]
        if job.session_title:
            lines.append(f"Session: {job.session_title}")
        if job.thread_id:
            lines.append(f"Thread: {job.thread_id}")
        if job.turn_id:
            lines.append(f"Turn: {job.turn_id}")
        if job.latest_message:
            lines.extend(["", "Latest update:", job.latest_message])
        if job.usage_summary:
            lines.extend(["", "Usage:", job.usage_summary])
        if isinstance(job.pending_user_input, PendingUserInputState):
            question = job.pending_user_input.questions[job.pending_user_input.current_index]
            lines.extend(["", "Waiting for input:", question.question])
        return "\n".join(lines)

    @staticmethod
    def _build_launch_request(
        state: SubmitSessionState,
        prompt: str,
    ) -> CodexLaunchRequest:
        if state.selected_directory is None:
            raise ValueError("No working directory was selected.")
        return CodexLaunchRequest(
            chat_id=state.chat_id,
            requested_action=state.requested_action,
            selected_directory=state.selected_directory,
            prompt=prompt,
            thread_id=state.selected_thread_id,
            session_title=state.selected_session_title,
        )

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

    async def _handle_request_user_input(
        self,
        running_job: RunningCodexJob | None,
        request: codex_app_server.RequestUserInputRequest,
    ) -> dict[str, object]:
        if running_job is None:
            raise RuntimeError("Telegram gateway cannot answer user input without a running job")

        response_future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        pending_request = PendingUserInputState(
            request_id=request.request_id,
            turn_id=request.turn_id,
            item_id=request.item_id,
            questions=request.questions,
            response_future=response_future,
        )
        running_job.pending_user_input = pending_request
        running_job.latest_status = "Waiting for user input"
        running_job.latest_message = request.questions[0].question
        response = await self._send_message(
            running_job.chat_id,
            self._request_user_input_text(pending_request),
            reply_markup=self._request_user_input_keyboard(pending_request),
        )
        pending_request.message_id = response.get("message_id")

        try:
            return await response_future
        finally:
            if running_job.pending_user_input is pending_request:
                running_job.pending_user_input = None
            if not response_future.done():
                response_future.cancel()

    async def _record_user_input_answer(
        self,
        running_job: RunningCodexJob,
        pending_request: PendingUserInputState,
        chat_id: str,
        message_id: int,
        option_index: int,
    ) -> None:
        question = pending_request.questions[pending_request.current_index]
        if option_index < 0 or option_index >= len(question.options):
            raise ValueError("Selected option is outside the available choices")

        selected_option = question.options[option_index]
        pending_request.answers[question.question_id] = [selected_option.label]

        if pending_request.current_index < len(pending_request.questions) - 1:
            pending_request.current_index += 1
            next_question = pending_request.questions[pending_request.current_index]
            running_job.latest_status = "Waiting for user input"
            running_job.latest_message = next_question.question
            await self._edit_message_text(
                chat_id,
                message_id,
                self._request_user_input_text(pending_request),
                reply_markup=self._request_user_input_keyboard(pending_request),
            )
            return

        running_job.latest_status = "Submitted user input"
        running_job.latest_message = "Submitted structured answers back to Codex."
        if not pending_request.response_future.done():
            pending_request.response_future.set_result(
                {
                    "answers": {
                        question_id: {"answers": answers}
                        for question_id, answers in pending_request.answers.items()
                    }
                }
            )
        await self._edit_message_text(
            chat_id,
            message_id,
            "Answers submitted. Codex is resuming.",
            reply_markup=None,
        )

    async def _steer_running_job(
        self,
        running_job: RunningCodexJob,
        prompt: str,
    ) -> None:
        process = running_job.process
        if process is None or not hasattr(process, "steer_turn"):
            await self._send_message(
                running_job.chat_id,
                "The current Codex task cannot accept follow-up input yet.",
            )
            return
        if running_job.thread_id is None or running_job.turn_id is None:
            await self._send_message(
                running_job.chat_id,
                "Codex has not exposed an active turn yet. Try again in a moment.",
            )
            return

        running_job.latest_status = "Steering active turn"
        running_job.latest_message = prompt
        try:
            running_job.turn_id = await process.steer_turn(
                prompt,
                thread_id=running_job.thread_id,
                turn_id=running_job.turn_id,
            )
        except Exception as exc:
            running_job.latest_status = "Steer failed"
            await self._send_message(
                running_job.chat_id,
                f"Failed to steer the active Codex turn:\n{exc}",
            )
            return

        running_job.latest_status = "Steer sent"
        await self._send_message(
            running_job.chat_id,
            "Follow-up sent to the active Codex turn.",
        )

    async def _run_turn_with_completion_guard(
        self,
        *,
        running_job: RunningCodexJob | None,
        process: codex_app_server.CodexAppServer,
        launch_request: CodexLaunchRequest,
        prompt: str,
    ) -> codex_app_server.CodexTurnResult:
        """Run one or more turns until away-mode completion approval is satisfied."""
        current_prompt = prompt
        current_thread_id = launch_request.thread_id

        while True:
            result = await process.run_turn(
                cwd=launch_request.selected_directory,
                prompt=current_prompt,
                thread_id=current_thread_id,
                on_event=lambda event: self._handle_codex_event(
                    running_job,
                    launch_request,
                    event,
                ),
                on_request_user_input=lambda request: self._handle_request_user_input(
                    running_job,
                    request,
                ),
            )
            current_thread_id = result.thread_id
            if running_job is not None:
                running_job.thread_id = result.thread_id

            if not is_user_away() or result.status != "completed" or not current_thread_id:
                return result

            thread_payload = await process.read_thread(
                current_thread_id,
                include_turns=True,
            )
            confirmation_state = detect_completion_confirmation_state(thread_payload)
            if confirmation_state == "approved":
                return result

            if running_job is not None:
                running_job.latest_status = "Awaiting completion confirmation"
                running_job.latest_message = (
                    "Completion approval missing; continuing the task."
                    if confirmation_state == "missing"
                    else "Completion was rejected; continuing the task."
                )

            if confirmation_state == "missing":
                gateway_debug_log(
                    "Away-mode completion guard reopening turn because confirmation "
                    f"tool was missing chat={launch_request.chat_id} thread={current_thread_id}"
                )
                current_prompt = build_missing_completion_confirmation_prompt()
                continue

            gateway_debug_log(
                "Away-mode completion guard reopening turn because completion "
                f"was rejected chat={launch_request.chat_id} thread={current_thread_id}"
            )
            current_prompt = build_rejected_completion_confirmation_prompt()

    async def _launch_codex_job_impl(
        self,
        launch_request: CodexLaunchRequest,
    ) -> None:
        running_job = RunningCodexJob(
            chat_id=launch_request.chat_id,
            selected_directory=launch_request.selected_directory,
            prompt=launch_request.prompt,
            requested_action=launch_request.requested_action,
            latest_status="Starting Codex MCP",
            thread_id=launch_request.thread_id,
            session_title=launch_request.session_title,
        )
        task = asyncio.create_task(
            self._run_codex_job(launch_request)
        )
        running_job.handle = task
        self.active_jobs[launch_request.chat_id] = running_job

    def _remember_loaded_session(self, job: RunningCodexJob) -> None:
        """Keep one loaded session available for later /tasks and /steer flows."""
        if not job.thread_id:
            return
        sessions = [
            existing
            for existing in self.loaded_sessions.get(job.chat_id, [])
            if existing.thread_id != job.thread_id
        ]
        sessions.insert(0, job)
        self.loaded_sessions[job.chat_id] = sessions

    async def _dispatch_steer_to_loaded_session(
        self,
        job: RunningCodexJob,
        prompt: str,
    ) -> None:
        """Send follow-up input to a loaded session, using steer when active."""
        if self._job_is_running(job):
            await self._steer_running_job(job, prompt)
            return

        if job.thread_id is None:
            raise RuntimeError("Loaded session is missing thread id")

        async def run_loaded_turn() -> None:
            process = job.process
            if not isinstance(process, codex_app_server.CodexAppServer):
                raise RuntimeError("Loaded session process is unavailable")
            self.active_jobs[job.chat_id] = job
            job.handle = asyncio.current_task()
            job.latest_status = "Starting follow-up turn"
            job.latest_message = prompt
            success = False
            result_text = ""
            try:
                follow_up_request = CodexLaunchRequest(
                    chat_id=job.chat_id,
                    requested_action="steer",
                    selected_directory=job.selected_directory,
                    prompt=prompt,
                    thread_id=job.thread_id,
                    session_title=job.session_title,
                )
                result = await self._run_turn_with_completion_guard(
                    running_job=job,
                    process=process,
                    launch_request=follow_up_request,
                    prompt=prompt,
                )
                job.thread_id = result.thread_id
                success = result.status == "completed"
                result_text = result.content or (result.error_message or "")
                job.latest_status = "idle" if success else result.status
                self._remember_loaded_session(job)
            except asyncio.CancelledError:
                self.active_jobs.pop(job.chat_id, None)
                raise
            except Exception as exc:
                result_text = str(exc).strip()
                job.latest_status = "systemError"
                self._remember_loaded_session(job)
            finally:
                self.active_jobs.pop(job.chat_id, None)

            await self._send_message(
                job.chat_id,
                self._completion_message(
                    success,
                    result_text,
                    job.selected_directory,
                    job.usage_summary,
                ),
            )

        job.handle = asyncio.create_task(run_loaded_turn())

    async def _run_codex_job(
        self,
        launch_request: CodexLaunchRequest,
    ) -> None:
        chat_id = launch_request.chat_id
        selected_directory = launch_request.selected_directory
        running_job = self.active_jobs.get(chat_id)
        app_server = self._app_server_factory()
        success = False
        result_text = ""

        try:
            if running_job is not None:
                running_job.process = app_server
                running_job.session_title = (
                    running_job.session_title
                    or self._session_title_from_prompt(launch_request.prompt)
                )
                running_job.latest_status = "Starting Codex app-server"

            prompt = (
                build_gateway_resume_prompt(
                    selected_directory,
                    launch_request.prompt,
                )
                if launch_request.thread_id
                else build_gateway_submit_prompt(
                    selected_directory,
                    launch_request.prompt,
                )
            )
            result = await self._run_turn_with_completion_guard(
                running_job=running_job,
                process=app_server,
                launch_request=launch_request,
                prompt=prompt,
            )

            if running_job is not None:
                running_job.thread_id = result.thread_id
            success = result.status == "completed"
            result_text = result.content or (result.error_message or "")
        except asyncio.CancelledError:
            if (
                running_job is not None
                and isinstance(running_job.pending_user_input, PendingUserInputState)
                and not running_job.pending_user_input.response_future.done()
            ):
                running_job.pending_user_input.response_future.cancel()
            await app_server.aclose()
            self.submit_sessions.pop(chat_id, None)
            self.active_jobs.pop(chat_id, None)
            raise
        except Exception as exc:
            result_text = str(exc).strip()
            if not result_text:
                result_text = "\n".join(app_server.stderr_lines).strip()
            await app_server.aclose()
        finally:
            if (
                running_job is not None
                and isinstance(running_job.pending_user_input, PendingUserInputState)
                and not running_job.pending_user_input.response_future.done()
            ):
                running_job.pending_user_input.response_future.cancel()

        self.submit_sessions.pop(chat_id, None)
        completed_job = self.active_jobs.pop(chat_id, None)
        if completed_job is not None and completed_job.termination_requested:
            await app_server.aclose()
            return
        if completed_job is not None and completed_job.thread_id:
            completed_job.latest_status = "idle" if success else completed_job.latest_status
            self._remember_loaded_session(completed_job)
        await self._send_message(
            chat_id,
            self._completion_message(
                success,
                result_text,
                selected_directory,
                completed_job.usage_summary if completed_job is not None else None,
            ),
        )

    def _handle_codex_event(
        self,
        running_job: RunningCodexJob | None,
        launch_request: CodexLaunchRequest,
        event: dict,
    ) -> None:
        if running_job is None:
            return
        apply_codex_event(running_job, event)
        running_job.session_title = (
            running_job.session_title
            or launch_request.session_title
            or self._session_title_from_prompt(launch_request.prompt)
        )

    @staticmethod
    def _session_title_from_prompt(prompt: str) -> str:
        first_line = next(
            (line.strip() for line in prompt.splitlines() if line.strip()),
            "Codex session",
        )
        return first_line[:80]

    async def _list_codex_threads_impl(self) -> list[codex_app_server.CodexThreadSummary]:
        app_server = self._app_server_factory()
        try:
            threads = await app_server.list_threads(limit=max(self.page_size * 5, 50))
        finally:
            await app_server.aclose()

        filtered_threads: list[codex_app_server.CodexThreadSummary] = []
        for thread in threads:
            try:
                resolved_cwd = resolve_directory(self.root, thread.cwd)
            except ValueError:
                continue
            filtered_threads.append(
                codex_app_server.CodexThreadSummary(
                    thread_id=thread.thread_id,
                    cwd=resolved_cwd,
                    preview=thread.preview,
                    name=thread.name,
                    updated_at=thread.updated_at,
                    status_type=thread.status_type,
                )
            )
        return filtered_threads[: self.page_size]

    @staticmethod
    def _job_is_running(job: RunningCodexJob) -> bool:
        if hasattr(job.handle, "done"):
            return not job.handle.done()
        return True


def build_gateway_submit_prompt(workdir: Path, prompt: str) -> str:
    """Inject Telegram-specific completion requirements ahead of the user task."""
    resolved_workdir = workdir.expanduser().resolve()
    return (
        "Telegram gateway requirements:\n"
        f"- You are working in project directory: {resolved_workdir}\n"
        "- Before you finish, you MUST call the `telegram_feedback` tool.\n"
        f"- Use `project_directory` set to `{resolved_workdir}`.\n"
        "- Provide a concise `summary` of the work, current state, and any question for the user.\n"
        f"- If the user is away, you must not finish a turn until `{COMPLETION_CONFIRMATION_TOOL_NAME}` returns approval.\n"
        f"- Use `{COMPLETION_CONFIRMATION_TOOL_NAME}` with a concise completion summary before stopping.\n"
        "- If completion is rejected, call `telegram_feedback` again to ask for the next instruction and continue.\n"
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
        f"- If the user is away, you must not finish a turn until `{COMPLETION_CONFIRMATION_TOOL_NAME}` returns approval.\n"
        f"- Use `{COMPLETION_CONFIRMATION_TOOL_NAME}` with a concise completion summary before stopping.\n"
        "- If completion is rejected, call `telegram_feedback` again to ask for the next instruction and continue.\n"
        "- Do NOT use `interactive_feedback`.\n"
        "- Do NOT finish the task until `telegram_feedback` returns.\n"
        "\n"
        "Additional instruction from user:\n"
        f"{prompt}"
    )


def extract_agent_message_text(message: dict | str) -> str:
    """Extract plain text from a Codex agent_message event payload."""
    if isinstance(message, str):
        return message.strip()
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
        ("inputTokens", "input"),
        ("output_tokens", "output"),
        ("outputTokens", "output"),
        ("reasoning_tokens", "reasoning"),
        ("reasoning_output_tokens", "reasoning"),
        ("reasoningOutputTokens", "reasoning"),
    ]
    parts = []
    for key, label in fields:
        value = usage.get(key)
        if isinstance(value, int):
            rendered = f"{label} {value}"
            if rendered not in parts:
                parts.append(rendered)
    if not parts:
        return None
    return " / ".join(parts)


def apply_codex_event(job: RunningCodexJob, event: dict) -> None:
    """Update the tracked task state from one Codex JSON event."""
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type not in {
        "turn.completed",
        "token_count",
        "server_request_resolved",
    }:
        job.latest_status = event_type

    thread_id = event.get("thread_id")
    if isinstance(thread_id, str) and thread_id.strip():
        job.thread_id = thread_id.strip()
    session_id = event.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        job.thread_id = session_id.strip()
    turn_id = event.get("turn_id")
    if isinstance(turn_id, str) and turn_id.strip():
        job.turn_id = turn_id.strip()

    if event_type == "agent_message":
        message = event.get("message")
        text = extract_agent_message_text(message) if isinstance(message, (dict, str)) else ""
        if text:
            job.latest_message = text
    elif event_type in {"agent_message_delta", "agent_message_content_delta"}:
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            job.latest_message = f"{job.latest_message}{delta}"
    elif event_type == "task_complete":
        turn = event.get("turn")
        if isinstance(turn, dict):
            completed_turn_id = turn.get("id")
            if isinstance(completed_turn_id, str) and completed_turn_id.strip():
                job.turn_id = completed_turn_id.strip()
        last_agent_message = event.get("last_agent_message")
        if isinstance(last_agent_message, str) and last_agent_message.strip():
            job.latest_message = last_agent_message.strip()

    usage = event.get("usage")
    if not isinstance(usage, dict):
        turn = event.get("turn")
        if isinstance(turn, dict):
            usage = turn.get("usage")
    if not isinstance(usage, dict):
        info = event.get("info")
        if isinstance(info, dict):
            usage = info.get("total_token_usage")
            if not isinstance(usage, dict):
                token_usage = info.get("tokenUsage")
                if isinstance(token_usage, dict):
                    usage = token_usage.get("total")
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
    required_commands = build_gateway_commands()
    required_names = {command["command"] for command in required_commands}
    merged_commands: list[dict[str, str]] = []
    command_index: dict[str, dict[str, str]] = {}

    for command in current_commands:
        if not isinstance(command, dict):
            continue
        name = command.get("command")
        if not isinstance(name, str) or not name:
            continue
        if name in MANAGED_GATEWAY_COMMAND_NAMES and name not in required_names:
            continue
        description = command.get("description")
        normalized = {
            "command": name,
            "description": description if isinstance(description, str) else "",
        }
        merged_commands.append(normalized)
        command_index[name] = normalized

    needs_update = any(
        isinstance(command, dict)
        and isinstance(command.get("command"), str)
        and command["command"] in MANAGED_GATEWAY_COMMAND_NAMES
        and command["command"] not in required_names
        for command in current_commands
    )
    for required_command in required_commands:
        existing = command_index.get(required_command["command"])
        if existing is None:
            merged_commands.append(dict(required_command))
            needs_update = True
        elif existing["description"] != required_command["description"]:
            existing["description"] = required_command["description"]
            needs_update = True

    if needs_update:
        await client.set_commands(merged_commands)
        gateway_debug_log(f"Updated Telegram slash commands: {required_commands}")
    else:
        gateway_debug_log("Telegram slash commands already up to date")


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
    gateway_debug_log(
        "Loaded Telegram gateway config "
        f"chat_id={config['chat_id']} root={Path(config['root']).expanduser()}"
    )
    client = TelegramBotClient(
        token=str(config["bot_token"]),
        api_base=str(config["api_base"]),
    )
    await ensure_gateway_commands(client)
    service = TelegramGatewayService(
        client=client,
        root=Path(config["root"]),
    )

    updates = await poll_updates_with_retry(client, timeout=0)
    next_offset = 0
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset, update_id)
    if next_offset:
        next_offset += 1

    target_chat_id = str(config["chat_id"])
    gateway_debug_log(f"Telegram gateway is polling updates for chat_id={target_chat_id}")

    while True:
        updates = await poll_updates_with_retry(
            client,
            offset=next_offset or None,
            timeout=30,
        )
        if updates:
            gateway_debug_log(f"Received {len(updates)} Telegram updates")
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
