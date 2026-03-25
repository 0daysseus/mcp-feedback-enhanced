#!/usr/bin/env python3
"""
Thin stdio JSON-RPC client for `codex app-server`.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class CodexThreadSummary:
    """One resumable Codex thread from the local Codex session history."""

    thread_id: str
    cwd: Path
    preview: str
    name: str | None = None
    updated_at: int | None = None
    status_type: str | None = None

    @property
    def display_name(self) -> str:
        if self.name and self.name.strip():
            return self.name.strip()
        if self.preview.strip():
            return self.preview.strip()
        return self.thread_id


@dataclass(slots=True)
class CodexTurnResult:
    """Final result for one app-server turn."""

    thread_id: str
    turn_id: str
    content: str
    status: str
    error_message: str | None = None


@dataclass(slots=True)
class RequestUserInputOption:
    """One selectable option for an app-server request_user_input question."""

    label: str
    description: str | None = None


@dataclass(slots=True)
class RequestUserInputQuestion:
    """One single-choice request_user_input question."""

    question_id: str
    header: str | None
    question: str
    options: tuple[RequestUserInputOption, ...]


@dataclass(slots=True)
class RequestUserInputRequest:
    """Structured item/tool/requestUserInput server request payload."""

    request_id: int | str
    thread_id: str | None
    turn_id: str | None
    item_id: str | None
    questions: tuple[RequestUserInputQuestion, ...]


class CodexAppServer:
    """Minimal client for listing Codex CLI threads through app-server."""

    def __init__(self, command: list[str] | None = None):
        self.command = command or ["codex", "app-server", "--listen", "stdio://"]
        self.process: asyncio.subprocess.Process | None = None
        self.stderr_lines: list[str] = []
        self._stderr_task: asyncio.Task | None = None
        self._request_id = 1
        self._initialized = False
        self._write_lock = asyncio.Lock()
        self._runtime_response_futures: dict[int, asyncio.Future[dict[str, object]]] = {}
        self._active_thread_id: str | None = None
        self._active_turn_id: str | None = None

    async def start(self) -> None:
        if self.process is not None:
            return

        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._consume_stderr())
        await self._initialize()

    async def list_threads(self, limit: int = 50) -> list[CodexThreadSummary]:
        result = await self.request(
            "thread/list",
            {
                "limit": limit,
                "sourceKinds": ["cli"],
            },
        )
        data = result.get("data")
        if not isinstance(data, list):
            return []

        summaries: list[CodexThreadSummary] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            thread_id = item.get("id")
            cwd = item.get("cwd")
            if not isinstance(thread_id, str) or not thread_id.strip():
                continue
            if not isinstance(cwd, str) or not cwd.strip():
                continue
            status = item.get("status")
            status_type = None
            if isinstance(status, dict):
                raw_status_type = status.get("type")
                if isinstance(raw_status_type, str) and raw_status_type.strip():
                    status_type = raw_status_type.strip()
            updated_at = item.get("updatedAt")
            summaries.append(
                CodexThreadSummary(
                    thread_id=thread_id.strip(),
                    cwd=Path(cwd).expanduser(),
                    preview=_as_clean_string(item.get("preview")),
                    name=_as_optional_string(item.get("name")),
                    updated_at=updated_at if isinstance(updated_at, int) else None,
                    status_type=status_type,
                )
            )
        return summaries

    async def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
    ) -> dict[str, object]:
        """Read one stored thread by id."""
        return await self.request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": include_turns,
            },
        )

    async def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        thread_id: str | None = None,
        on_event: Callable[[dict[str, object]], None] | None = None,
        on_request_user_input: Callable[
            [RequestUserInputRequest], Awaitable[dict[str, object]]
        ]
        | None = None,
    ) -> CodexTurnResult:
        active_thread_id = thread_id
        resumed_existing_thread = active_thread_id is not None
        if active_thread_id is None:
            thread_result = await self.request(
                "thread/start",
                {"cwd": str(cwd.expanduser().resolve())},
                on_event=on_event,
                on_request_user_input=on_request_user_input,
            )
            active_thread_id = extract_thread_id(thread_result)
        else:
            await self.request(
                "thread/resume",
                {"threadId": active_thread_id},
                on_event=on_event,
                on_request_user_input=on_request_user_input,
            )

        if active_thread_id is None:
            raise RuntimeError("Codex app-server did not return a thread id")
        self._active_thread_id = active_thread_id

        if on_event is not None and resumed_existing_thread:
            on_event(
                {
                    "type": "session_configured",
                    "thread_id": active_thread_id,
                    "session_id": active_thread_id,
                }
            )

        turn_result = await self.request(
            "turn/start",
            {
                "threadId": active_thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": str(cwd.expanduser().resolve()),
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "workspaceWrite"},
            },
            on_event=on_event,
            on_request_user_input=on_request_user_input,
        )
        turn = turn_result.get("turn")
        if not isinstance(turn, dict):
            raise RuntimeError("Codex app-server did not return a turn payload")
        turn_id = extract_turn_id(turn)
        if turn_id is None:
            raise RuntimeError("Codex app-server did not return a turn id")
        self._active_turn_id = turn_id

        latest_agent_message = ""
        while True:
            message = await self._read_message()
            if self._resolve_runtime_response(message):
                continue
            handled_request = await self._handle_server_request(
                message,
                on_request_user_input=on_request_user_input,
            )
            if handled_request:
                continue
            normalized = normalize_notification_message(message)
            if normalized is not None:
                if normalized.get("type") == "agent_message_delta":
                    delta = normalized.get("delta")
                    if isinstance(delta, str):
                        latest_agent_message = f"{latest_agent_message}{delta}"
                elif normalized.get("type") == "agent_message":
                    message_text = normalized.get("message")
                    if isinstance(message_text, str):
                        latest_agent_message = message_text
                if on_event is not None:
                    on_event(normalized)

            if message.get("method") != "turn/completed":
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            completed_turn = params.get("turn")
            if not isinstance(completed_turn, dict):
                continue
            completed_turn_id = extract_turn_id(completed_turn)
            if completed_turn_id != turn_id:
                continue

            status = completed_turn.get("status")
            status_value = status if isinstance(status, str) else "failed"
            error_message = extract_turn_error(completed_turn)
            if not latest_agent_message:
                latest_agent_message = extract_turn_agent_message(completed_turn)
            self._active_turn_id = None
            return CodexTurnResult(
                thread_id=active_thread_id,
                turn_id=turn_id,
                content=latest_agent_message.strip(),
                status=status_value,
                error_message=error_message,
            )

    async def steer_turn(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> str:
        await self.start()
        active_thread_id = thread_id or self._active_thread_id
        active_turn_id = turn_id or self._active_turn_id
        if active_thread_id is None or active_turn_id is None:
            raise RuntimeError("No active Codex turn is available to steer")

        request_id = self._next_request_id()
        response_future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        self._runtime_response_futures[request_id] = response_future
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "turn/steer",
                "params": {
                    "threadId": active_thread_id,
                    "input": [{"type": "text", "text": prompt}],
                    "expectedTurnId": active_turn_id,
                },
            }
        )

        try:
            result = await response_future
        finally:
            self._runtime_response_futures.pop(request_id, None)

        returned_turn_id = result.get("turnId")
        if isinstance(returned_turn_id, str) and returned_turn_id.strip():
            self._active_turn_id = returned_turn_id.strip()
            return self._active_turn_id
        raise RuntimeError("Codex app-server returned an invalid steer response")

    async def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        on_event: Callable[[dict[str, object]], None] | None = None,
        on_request_user_input: Callable[
            [RequestUserInputRequest], Awaitable[dict[str, object]]
        ]
        | None = None,
    ) -> dict[str, object]:
        await self.start()
        request_id = self._next_request_id()
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )

        while True:
            message = await self._read_message()
            if message.get("id") != request_id:
                handled_request = await self._handle_server_request(
                    message,
                    on_request_user_input=on_request_user_input,
                )
                if handled_request:
                    continue
                normalized = normalize_notification_message(message)
                if normalized is not None and on_event is not None:
                    on_event(normalized)
                continue
            if "error" in message:
                error = message.get("error")
                raise RuntimeError(f"{method} failed: {error}")
            result = message.get("result")
            if isinstance(result, dict):
                return result
            raise RuntimeError(f"{method} returned an invalid result payload")

    async def notify(self, method: str, params: dict[str, object] | None = None) -> None:
        await self.start()
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def aclose(self) -> None:
        for response_future in self._runtime_response_futures.values():
            if not response_future.done():
                response_future.cancel()
        self._runtime_response_futures.clear()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        self._active_turn_id = None

    async def _initialize(self) -> None:
        if self._initialized:
            return
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "mcp-feedback-enhanced",
                    "title": "mcp-feedback-enhanced",
                    "version": "2.6.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                },
            },
        )
        await self.notify("initialized", {})
        self._initialized = True

    async def _handle_server_request(
        self,
        message: dict[str, object],
        *,
        on_request_user_input: Callable[
            [RequestUserInputRequest], Awaitable[dict[str, object]]
        ]
        | None,
    ) -> bool:
        request = extract_request_user_input_request(message)
        if request is None:
            return False
        if on_request_user_input is None:
            raise RuntimeError(
                "Codex app-server requested user input but no Telegram handler was configured"
            )
        result = await on_request_user_input(request)
        await self._write_message(
            {
                "jsonrpc": "2.0",
                "id": request.request_id,
                "result": result,
            }
        )
        return True

    async def _write_message(self, payload: dict[str, object]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Codex app-server is not running")
        async with self._write_lock:
            body = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
            self.process.stdin.write(body)
            await self.process.stdin.drain()

    async def _read_message(self) -> dict[str, object]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Codex app-server is not running")
        line = await self.process.stdout.readline()
        if not line:
            raise RuntimeError("Codex app-server exited before sending a response")
        payload = json.loads(line.decode("utf-8", errors="replace").strip())
        if not isinstance(payload, dict):
            raise RuntimeError("Codex app-server returned a non-object payload")
        return payload

    async def _consume_stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self.stderr_lines.append(text)

    def _next_request_id(self) -> int:
        request_id = self._request_id
        self._request_id += 1
        return request_id

    def _resolve_runtime_response(self, message: dict[str, object]) -> bool:
        response_id = message.get("id")
        if not isinstance(response_id, int):
            return False
        response_future = self._runtime_response_futures.get(response_id)
        if response_future is None:
            return False
        if "error" in message:
            if not response_future.done():
                response_future.set_exception(RuntimeError(f"turn/steer failed: {message['error']}"))
            return True
        result = message.get("result")
        if not isinstance(result, dict):
            if not response_future.done():
                response_future.set_exception(
                    RuntimeError("turn/steer returned an invalid result payload")
                )
            return True
        if not response_future.done():
            response_future.set_result(result)
        return True


def _as_clean_string(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _as_optional_string(value: object) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def extract_thread_id(result: dict[str, object]) -> str | None:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        return None
    thread_id = thread.get("id")
    if isinstance(thread_id, str) and thread_id.strip():
        return thread_id.strip()
    return None


def extract_turn_id(turn: dict[str, object]) -> str | None:
    turn_id = turn.get("id")
    if isinstance(turn_id, str) and turn_id.strip():
        return turn_id.strip()
    return None


def extract_turn_error(turn: dict[str, object]) -> str | None:
    error = turn.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return None


def extract_turn_agent_message(turn: dict[str, object]) -> str:
    items = turn.get("items")
    if not isinstance(items, list):
        return ""
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def extract_request_user_input_request(
    message: dict[str, object],
) -> RequestUserInputRequest | None:
    if message.get("method") != "item/tool/requestUserInput":
        return None

    request_id = message.get("id")
    if not isinstance(request_id, int | str):
        return None

    params = message.get("params")
    if not isinstance(params, dict):
        return None

    raw_questions = params.get("questions")
    if not isinstance(raw_questions, list):
        return None

    questions: list[RequestUserInputQuestion] = []
    for raw_question in raw_questions:
        if not isinstance(raw_question, dict):
            continue
        question_id = _as_optional_string(raw_question.get("id"))
        question_text = _as_clean_string(raw_question.get("question"))
        raw_options = raw_question.get("options")
        if question_id is None or not question_text or not isinstance(raw_options, list):
            continue

        options: list[RequestUserInputOption] = []
        for raw_option in raw_options:
            if not isinstance(raw_option, dict):
                continue
            label = _as_clean_string(raw_option.get("label"))
            if not label:
                continue
            options.append(
                RequestUserInputOption(
                    label=label,
                    description=_as_optional_string(raw_option.get("description")),
                )
            )

        if not options:
            continue

        questions.append(
            RequestUserInputQuestion(
                question_id=question_id,
                header=_as_optional_string(raw_question.get("header")),
                question=question_text,
                options=tuple(options),
            )
        )

    if not questions:
        return None

    return RequestUserInputRequest(
        request_id=request_id,
        thread_id=_as_optional_string(params.get("threadId")),
        turn_id=_as_optional_string(params.get("turnId")),
        item_id=_as_optional_string(params.get("itemId")),
        questions=tuple(questions),
    )


def normalize_notification_message(message: dict[str, object]) -> dict[str, object] | None:
    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return None

    if method == "thread/started":
        thread = params.get("thread")
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str) and thread_id.strip():
                return {
                    "type": "session_configured",
                    "thread_id": thread_id.strip(),
                    "session_id": thread_id.strip(),
                }

    if method == "mcpServer/startupStatus/updated":
        name = params.get("name")
        status = params.get("status")
        if isinstance(name, str) and isinstance(status, str):
            return {
                "type": "mcp_startup_update",
                "server": name,
                "status": {"state": status},
            }

    if method == "turn/started":
        turn = params.get("turn")
        if isinstance(turn, dict):
            return {
                "type": "task_started",
                "turn_id": extract_turn_id(turn),
            }

    if method == "turn/completed":
        turn = params.get("turn")
        if isinstance(turn, dict):
            return {
                "type": "task_complete",
                "turn": turn,
                "last_agent_message": extract_turn_agent_message(turn),
            }

    if method == "thread/tokenUsage/updated":
        return {
            "type": "token_count",
            "info": dict(params),
        }

    if method == "item/agentMessage/delta":
        delta = params.get("delta")
        if isinstance(delta, str):
            return {
                "type": "agent_message_delta",
                "delta": delta,
            }

    if method == "item/completed":
        item = params.get("item")
        if isinstance(item, dict) and item.get("type") == "agentMessage":
            text = item.get("text")
            if isinstance(text, str):
                return {
                    "type": "agent_message",
                    "message": text,
                }

    if method == "serverRequest/resolved":
        request_id = params.get("requestId")
        if isinstance(request_id, int | str):
            return {
                "type": "server_request_resolved",
                "request_id": request_id,
                "thread_id": _as_optional_string(params.get("threadId")),
            }

    return None
