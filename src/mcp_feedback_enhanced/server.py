#!/usr/bin/env python3
"""
MCP Feedback Enhanced 伺服器主要模組

此模組提供 MCP (Model Context Protocol) 的增強回饋收集功能，
支援智能環境檢測，自動使用 Web UI 介面。

主要功能：
- MCP 工具實現
- 介面選擇（Web UI）
- 環境檢測 (SSH Remote, WSL, Local)
- 國際化支援
- 圖片處理與上傳
- 命令執行與結果展示
- 專案目錄管理

主要 MCP 工具：
- interactive_feedback: 收集用戶互動回饋
- telegram_feedback: 收集 Telegram 回饋
- get_system_info: 獲取系統環境資訊

作者: Fábio Ferreira (原作者)
增強: Minidoracat (Web UI, 圖片支援, 環境檢測)
重構: 模塊化設計
"""

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.utilities.types import Image as MCPImage
from mcp.types import TextContent
from pydantic import Field

# 導入統一的調試功能
from .debug import server_debug_log as debug_log
from .feedback_routing import is_user_away
from .telegram import (
    TelegramBotClient,
    TelegramClientError,
    TelegramFeedbackCancelled,
    TelegramFeedbackSession,
    build_completion_confirmation_keyboard,
    create_completion_confirmation_request,
    create_pending_feedback_request,
    discard_pending_feedback_request,
    register_completion_confirmation_message,
    register_pending_feedback_message,
    wait_for_completion_confirmation,
    wait_for_pending_feedback,
)

# 導入多語系支援
# 導入錯誤處理框架
from .utils.error_handler import ErrorHandler, ErrorType

# 導入資源管理器
from .utils.resource_manager import create_temp_file
from .web.utils.browser import find_remote_browser_helper


# ===== 編碼初始化 =====
def init_encoding():
    """初始化編碼設置，確保正確處理中文字符"""
    try:
        # Windows 特殊處理
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415

            # 設置為二進制模式
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

            # 重新包裝為 UTF-8 文本流，並禁用緩衝
            # 修復 union-attr 錯誤 - 安全獲取 buffer 或 detach
            stdin_buffer = getattr(sys.stdin, "buffer", None)
            if stdin_buffer is None and hasattr(sys.stdin, "detach"):
                stdin_buffer = sys.stdin.detach()

            stdout_buffer = getattr(sys.stdout, "buffer", None)
            if stdout_buffer is None and hasattr(sys.stdout, "detach"):
                stdout_buffer = sys.stdout.detach()

            sys.stdin = io.TextIOWrapper(
                stdin_buffer, encoding="utf-8", errors="replace", newline=None
            )
            sys.stdout = io.TextIOWrapper(
                stdout_buffer,
                encoding="utf-8",
                errors="replace",
                newline="",
                write_through=True,  # 關鍵：禁用寫入緩衝
            )
        else:
            # 非 Windows 系統的標準設置
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stdin, "reconfigure"):
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")

        # 設置 stderr 編碼（用於調試訊息）
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

        return True
    except Exception:
        # 如果編碼設置失敗，嘗試基本設置
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stdin, "reconfigure"):
                sys.stdin.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except:
            pass
        return False


# 初始化編碼（在導入時就執行）
_encoding_initialized = init_encoding()

# ===== 常數定義 =====
SERVER_NAME = "互動式回饋收集 MCP"
SSH_ENV_VARS = ["SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"]
REMOTE_ENV_VARS = ["REMOTE_CONTAINERS", "CODESPACES"]
REQUIRED_TELEGRAM_COMMANDS = [
    {"command": "done", "description": "Submit feedback"},
]
MANAGED_TELEGRAM_COMMAND_NAMES = {"done", "cancel"}
DEFAULT_FEEDBACK_TIMEOUT = 600
FEEDBACK_TIMEOUT_ENV_VAR = "MCP_FEEDBACK_TIMEOUT"
TELEGRAM_GATEWAY_RESTART_DELAY_SECONDS = 5.0
_http_telegram_gateway_thread: threading.Thread | None = None
_http_telegram_gateway_lock = threading.Lock()


class InteractiveFeedbackPrerequisiteError(RuntimeError):
    """Raised when interactive feedback cannot run in the current environment."""


# 初始化 MCP 服務器
from . import __version__


# 確保 log_level 設定為正確的大寫格式
fastmcp_settings = {}

# 檢查環境變數並設定正確的 log_level
env_log_level = os.getenv("FASTMCP_LOG_LEVEL", "").upper()
if env_log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    fastmcp_settings["log_level"] = env_log_level
else:
    # 預設使用 INFO 等級
    fastmcp_settings["log_level"] = "INFO"

mcp: Any = FastMCP(SERVER_NAME)


# ===== 工具函數 =====
def is_wsl_environment() -> bool:
    """
    檢測是否在 WSL (Windows Subsystem for Linux) 環境中運行

    Returns:
        bool: True 表示 WSL 環境，False 表示其他環境
    """
    try:
        # 檢查 /proc/version 文件是否包含 WSL 標識
        if os.path.exists("/proc/version"):
            with open("/proc/version") as f:
                version_info = f.read().lower()
                if "microsoft" in version_info or "wsl" in version_info:
                    debug_log("偵測到 WSL 環境（通過 /proc/version）")
                    return True

        # 檢查 WSL 相關環境變數
        wsl_env_vars = ["WSL_DISTRO_NAME", "WSL_INTEROP", "WSLENV"]
        for env_var in wsl_env_vars:
            if os.getenv(env_var):
                debug_log(f"偵測到 WSL 環境變數: {env_var}")
                return True

        # 檢查是否存在 WSL 特有的路徑
        wsl_paths = ["/mnt/c", "/mnt/d", "/proc/sys/fs/binfmt_misc/WSLInterop"]
        for path in wsl_paths:
            if os.path.exists(path):
                debug_log(f"偵測到 WSL 特有路徑: {path}")
                return True

    except Exception as e:
        debug_log(f"WSL 檢測過程中發生錯誤: {e}")

    return False


def is_remote_environment() -> bool:
    """
    檢測是否在遠端環境中運行

    Returns:
        bool: True 表示遠端環境，False 表示本地環境
    """
    # WSL 不應被視為遠端環境，因為它可以訪問 Windows 瀏覽器
    if is_wsl_environment():
        debug_log("WSL 環境不被視為遠端環境")
        return False

    # 檢查 SSH 連線指標
    for env_var in SSH_ENV_VARS:
        if os.getenv(env_var):
            debug_log(f"偵測到 SSH 環境變數: {env_var}")
            return True

    # 檢查遠端開發環境
    for env_var in REMOTE_ENV_VARS:
        if os.getenv(env_var):
            debug_log(f"偵測到遠端開發環境: {env_var}")
            return True

    # 檢查 Docker 容器
    if os.path.exists("/.dockerenv"):
        debug_log("偵測到 Docker 容器環境")
        return True

    # Windows 遠端桌面檢查
    if sys.platform == "win32":
        session_name = os.getenv("SESSIONNAME", "")
        if session_name and "RDP" in session_name:
            debug_log(f"偵測到 Windows 遠端桌面: {session_name}")
            return True

    # Linux 無顯示環境檢查（但排除 WSL）
    if (
        sys.platform.startswith("linux")
        and not os.getenv("DISPLAY")
        and not is_wsl_environment()
    ):
        debug_log("偵測到 Linux 無顯示環境")
        return True

    return False


def save_feedback_to_file(feedback_data: dict, file_path: str | None = None) -> str:
    """
    將回饋資料儲存到 JSON 文件

    Args:
        feedback_data: 回饋資料字典
        file_path: 儲存路徑，若為 None 則自動產生臨時文件

    Returns:
        str: 儲存的文件路徑
    """
    if file_path is None:
        # 使用資源管理器創建臨時文件
        file_path = create_temp_file(suffix=".json", prefix="feedback_")

    # 確保目錄存在
    directory = os.path.dirname(file_path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)

    # 複製數據以避免修改原始數據
    json_data = feedback_data.copy()

    # 處理圖片數據：將 bytes 轉換為 base64 字符串以便 JSON 序列化
    if "images" in json_data and isinstance(json_data["images"], list):
        processed_images = []
        for img in json_data["images"]:
            if isinstance(img, dict) and "data" in img:
                processed_img = img.copy()
                # 如果 data 是 bytes，轉換為 base64 字符串
                if isinstance(img["data"], bytes):
                    processed_img["data"] = base64.b64encode(img["data"]).decode(
                        "utf-8"
                    )
                    processed_img["data_type"] = "base64"
                processed_images.append(processed_img)
            else:
                processed_images.append(img)
        json_data["images"] = processed_images

    # 儲存資料
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    debug_log(f"回饋資料已儲存至: {file_path}")
    return file_path


def _find_git_repo_root(start_path: Path) -> Path | None:
    """Find the nearest git repository root from a starting path."""
    resolved_path = start_path.resolve()

    for candidate in (resolved_path, *resolved_path.parents):
        if (candidate / ".git").exists():
            return candidate

    return None


def _run_git_command(repo_root: Path, args: list[str]) -> str:
    """Run a git command in the given repository and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        error_message = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise RuntimeError(error_message)

    return result.stdout.rstrip("\n")


def _collect_source_debug_info() -> dict[str, Any]:
    """Collect source location and git metadata for runtime debugging."""
    server_file = Path(__file__).resolve()
    package_root = server_file.parent
    source_info: dict[str, Any] = {
        "來源模式": "packaged_copy",
        "server_file": str(server_file),
        "package_root": str(package_root),
        "git_repo_root": None,
        "git_commit": None,
        "git_branch": None,
        "git_dirty": None,
        "git_status_short": None,
    }

    repo_root = _find_git_repo_root(package_root)
    if repo_root is None:
        return source_info

    source_info["來源模式"] = "git_worktree"
    source_info["git_repo_root"] = str(repo_root.resolve())

    try:
        git_commit = _run_git_command(repo_root, ["rev-parse", "HEAD"])
        git_branch = _run_git_command(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
        git_status_short = _run_git_command(repo_root, ["status", "--short"])
    except Exception as exc:
        source_info["git_error"] = str(exc)
        return source_info

    source_info["git_commit"] = git_commit
    source_info["git_branch"] = git_branch
    source_info["git_dirty"] = bool(git_status_short)
    source_info["git_status_short"] = git_status_short

    return source_info


def _collect_browser_runtime_debug_info(manager: Any | None = None) -> dict[str, Any]:
    """Collect browser-launch diagnostics for the current runtime."""
    browser_info: dict[str, Any] = {
        "home": str(Path.home()),
        "browser_env": os.getenv("BROWSER"),
        "remote_browser_helper": find_remote_browser_helper(),
        "webui_manager_initialized": False,
        "has_current_session": False,
        "current_session_id": None,
        "current_session_has_websocket": False,
        "current_session_last_heartbeat": None,
        "global_active_tabs_count": 0,
        "pending_session_update": False,
        "last_browser_launch_attempt": None,
    }

    try:
        if manager is None:
            from .web import main as web_main  # noqa: PLC0415

            manager = getattr(web_main, "_web_ui_manager", None)

        if manager is None:
            return browser_info

        session = manager.get_current_session()
        browser_info["webui_manager_initialized"] = True
        browser_info["has_current_session"] = session is not None
        browser_info["global_active_tabs_count"] = manager.get_global_active_tabs_count()
        browser_info["pending_session_update"] = getattr(
            manager, "_pending_session_update", False
        )
        browser_info["last_browser_launch_attempt"] = getattr(
            manager, "last_browser_launch_attempt", None
        )

        if session is not None:
            browser_info["current_session_id"] = session.session_id
            browser_info["current_session_has_websocket"] = session.websocket is not None
            browser_info["current_session_last_heartbeat"] = getattr(
                session, "last_heartbeat", None
            )

    except Exception as exc:
        browser_info["browser_runtime_error"] = str(exc)

    return browser_info


def create_feedback_text(feedback_data: dict) -> str:
    """
    建立格式化的回饋文字

    Args:
        feedback_data: 回饋資料字典

    Returns:
        str: 格式化後的回饋文字
    """
    text_parts = []

    # 基本回饋內容
    if feedback_data.get("interactive_feedback"):
        text_parts.append(f"=== 用戶回饋 ===\n{feedback_data['interactive_feedback']}")

    # 命令執行日誌
    if feedback_data.get("command_logs"):
        text_parts.append(f"=== 命令執行日誌 ===\n{feedback_data['command_logs']}")

    # 圖片附件概要
    if feedback_data.get("images"):
        images = feedback_data["images"]
        text_parts.append(f"=== 圖片附件概要 ===\n用戶提供了 {len(images)} 張圖片：")

        for i, img in enumerate(images, 1):
            size = img.get("size", 0)
            name = img.get("name", "unknown")

            # 智能單位顯示
            if size < 1024:
                size_str = f"{size} B"
            elif size < 1024 * 1024:
                size_kb = size / 1024
                size_str = f"{size_kb:.1f} KB"
            else:
                size_mb = size / (1024 * 1024)
                size_str = f"{size_mb:.1f} MB"

            img_info = f"  {i}. {name} ({size_str})"

            # 為提高兼容性，添加 base64 預覽信息
            if img.get("data"):
                try:
                    if isinstance(img["data"], bytes):
                        img_base64 = base64.b64encode(img["data"]).decode("utf-8")
                    elif isinstance(img["data"], str):
                        img_base64 = img["data"]
                    else:
                        img_base64 = None

                    if img_base64:
                        # 只顯示前50個字符的預覽
                        preview = (
                            img_base64[:50] + "..."
                            if len(img_base64) > 50
                            else img_base64
                        )
                        img_info += f"\n     Base64 預覽: {preview}"
                        img_info += f"\n     完整 Base64 長度: {len(img_base64)} 字符"

                        # 如果 AI 助手不支援 MCP 圖片，可以提供完整 base64
                        debug_log(f"圖片 {i} Base64 已準備，長度: {len(img_base64)}")

                        # 檢查是否啟用 Base64 詳細模式（從 UI 設定中獲取）
                        include_full_base64 = feedback_data.get("settings", {}).get(
                            "enable_base64_detail", False
                        )

                        if include_full_base64:
                            # 根據檔案名推斷 MIME 類型
                            file_name = img.get("name", "image.png")
                            if file_name.lower().endswith((".jpg", ".jpeg")):
                                mime_type = "image/jpeg"
                            elif file_name.lower().endswith(".gif"):
                                mime_type = "image/gif"
                            elif file_name.lower().endswith(".webp"):
                                mime_type = "image/webp"
                            else:
                                mime_type = "image/png"

                            img_info += f"\n     完整 Base64: data:{mime_type};base64,{img_base64}"

                except Exception as e:
                    debug_log(f"圖片 {i} Base64 處理失敗: {e}")

            text_parts.append(img_info)

        # 添加兼容性說明
        text_parts.append(
            "\n💡 注意：如果 AI 助手無法顯示圖片，圖片數據已包含在上述 Base64 信息中。"
        )

    return "\n\n".join(text_parts) if text_parts else "用戶未提供任何回饋內容。"


def process_images(images_data: list[dict]) -> list[MCPImage]:
    """
    處理圖片資料，轉換為 MCP 圖片對象

    Args:
        images_data: 圖片資料列表

    Returns:
        List[MCPImage]: MCP 圖片對象列表
    """
    mcp_images = []

    for i, img in enumerate(images_data, 1):
        try:
            if not img.get("data"):
                debug_log(f"圖片 {i} 沒有資料，跳過")
                continue

            # 檢查數據類型並相應處理
            if isinstance(img["data"], bytes):
                # 如果是原始 bytes 數據，直接使用
                image_bytes = img["data"]
                debug_log(
                    f"圖片 {i} 使用原始 bytes 數據，大小: {len(image_bytes)} bytes"
                )
            elif isinstance(img["data"], str):
                # 如果是 base64 字符串，進行解碼
                image_bytes = base64.b64decode(img["data"])
                debug_log(f"圖片 {i} 從 base64 解碼，大小: {len(image_bytes)} bytes")
            else:
                debug_log(f"圖片 {i} 數據類型不支援: {type(img['data'])}")
                continue

            if len(image_bytes) == 0:
                debug_log(f"圖片 {i} 數據為空，跳過")
                continue

            # 根據文件名推斷格式
            file_name = img.get("name", "image.png")
            if file_name.lower().endswith((".jpg", ".jpeg")):
                image_format = "jpeg"
            elif file_name.lower().endswith(".gif"):
                image_format = "gif"
            else:
                image_format = "png"  # 默認使用 PNG

            # 創建 MCPImage 對象
            mcp_image = MCPImage(data=image_bytes, format=image_format)
            mcp_images.append(mcp_image)

            debug_log(f"圖片 {i} ({file_name}) 處理成功，格式: {image_format}")

        except Exception as e:
            # 使用統一錯誤處理（不影響 JSON RPC）
            error_id = ErrorHandler.log_error_with_context(
                e,
                context={"operation": "圖片處理", "image_index": i},
                error_type=ErrorType.FILE_IO,
            )
            debug_log(f"圖片 {i} 處理失敗 [錯誤ID: {error_id}]: {e}")

    debug_log(f"共處理 {len(mcp_images)} 張圖片")
    return mcp_images


def build_feedback_items(result: dict | None) -> list:
    """Build MCP feedback items from a channel result payload."""
    if not result:
        return [TextContent(type="text", text="用戶取消了回饋。")]

    feedback_items = []

    if (
        result.get("interactive_feedback")
        or result.get("command_logs")
        or result.get("images")
    ):
        feedback_text = create_feedback_text(result)
        feedback_items.append(TextContent(type="text", text=feedback_text))
        debug_log("文字回饋已添加")

    if result.get("images"):
        mcp_images = process_images(result["images"])
        feedback_items.extend(mcp_images)
        debug_log(f"已添加 {len(mcp_images)} 張圖片")

    if not feedback_items:
        feedback_items.append(TextContent(type="text", text="用戶未提供任何回饋內容。"))

    return feedback_items


def _resolve_feedback_timeout() -> int:
    """Resolve feedback timeout from environment.

    Timeout is controlled by ``MCP_FEEDBACK_TIMEOUT`` only.
    Invalid, empty, or non-positive values fall back to default timeout.
    """
    raw_timeout = os.getenv(FEEDBACK_TIMEOUT_ENV_VAR, "").strip()
    if not raw_timeout:
        return DEFAULT_FEEDBACK_TIMEOUT

    try:
        timeout = int(raw_timeout)
    except ValueError:
        debug_log(
            f"{FEEDBACK_TIMEOUT_ENV_VAR} 值無效 ({raw_timeout})，使用預設值 {DEFAULT_FEEDBACK_TIMEOUT}"
        )
        return DEFAULT_FEEDBACK_TIMEOUT

    if timeout <= 0:
        debug_log(
            f"{FEEDBACK_TIMEOUT_ENV_VAR} 值必須大於 0 ({timeout})，使用預設值 {DEFAULT_FEEDBACK_TIMEOUT}"
        )
        return DEFAULT_FEEDBACK_TIMEOUT

    return timeout


def _normalize_project_directory(project_directory: str) -> str:
    """Normalize a project directory argument to an absolute existing path."""
    if not os.path.exists(project_directory):
        project_directory = os.getcwd()
    return os.path.abspath(project_directory)


def _build_interactive_feedback_away_message() -> str:
    """Build retry guidance for away mode."""
    return (
        "interactive_feedback 当前已被 away 模式禁用。\n"
        "请直接调用 telegram_feedback 工具继续收集反馈。"
    )


def _build_interactive_feedback_timeout_message(timeout: int) -> str:
    """Build retry guidance for interactive feedback web timeouts."""
    return (
        "interactive_feedback 首次尝试通过 Web UI 收集反馈已超时 (timeout)."
        f"等待 {timeout} 秒后仍未收到用户回复。\n"
        "请改为调用 telegram_feedback 工具继续收集反馈。"
    )


def _build_completion_confirmation_prompt(summary: str, project_directory: str) -> str:
    """Build the Telegram message used for task-completion confirmation."""
    return (
        "Task completion check:\n"
        f"{summary}\n\n"
        "Project directory:\n"
        f"{project_directory}\n\n"
        "Should the agent stop now?"
    )


async def ensure_telegram_commands(client: TelegramBotClient) -> None:
    """Ensure the Telegram bot exposes the slash commands used by the feedback flow."""
    current_commands = await client.get_commands()
    required_names = {command["command"] for command in REQUIRED_TELEGRAM_COMMANDS}
    merged_commands: list[dict[str, str]] = []
    command_index: dict[str, dict[str, str]] = {}

    for command in current_commands:
        if not isinstance(command, dict):
            continue

        name = command.get("command")
        if not isinstance(name, str) or not name:
            continue
        if name in MANAGED_TELEGRAM_COMMAND_NAMES and name not in required_names:
            continue

        description = command.get("description")
        normalized_command = {
            "command": name,
            "description": description if isinstance(description, str) else "",
        }
        merged_commands.append(normalized_command)
        command_index[name] = normalized_command

    needs_update = any(
        isinstance(command, dict)
        and isinstance(command.get("command"), str)
        and command["command"] in MANAGED_TELEGRAM_COMMAND_NAMES
        and command["command"] not in required_names
        for command in current_commands
    )
    for required_command in REQUIRED_TELEGRAM_COMMANDS:
        existing_command = command_index.get(required_command["command"])
        if existing_command is None:
            merged_commands.append(dict(required_command))
            needs_update = True
            continue

        if existing_command["description"] != required_command["description"]:
            existing_command["description"] = required_command["description"]
            needs_update = True

    if needs_update:
        await client.set_commands(merged_commands)
        debug_log("Telegram commands registered or updated")
    else:
        debug_log("Telegram commands already up to date")


# ===== MCP 工具定義 =====
async def _interactive_feedback_impl(
    project_directory: str,
    summary: str,
    *,
    raise_on_timeout: bool = False,
) -> list:
    """Collect feedback through Web UI, optionally surfacing timeout for fallback."""
    is_remote = is_remote_environment()
    is_wsl = is_wsl_environment()

    debug_log(f"環境偵測結果 - 遠端: {is_remote}, WSL: {is_wsl}")
    debug_log("使用介面: Web UI")

    project_directory = _normalize_project_directory(project_directory)
    timeout = _resolve_feedback_timeout()

    try:
        from fastmcp.server.dependencies import get_http_headers  # noqa: PLC0415

        from .utils.request_env import (  # noqa: PLC0415
            extract_browser_env_overrides_from_headers,
            request_env_overrides,
        )

        debug_log("回饋模式: web")

        headers = get_http_headers()
        env_overrides = extract_browser_env_overrides_from_headers(headers)
        if env_overrides:
            debug_log(
                "從 HTTP headers 套用瀏覽器環境變數覆寫: "
                f"{sorted(env_overrides.keys())}"
            )

        desktop_mode = os.getenv("MCP_DESKTOP_MODE", "").lower() == "true"
        display_env = os.getenv("DISPLAY", "").strip()

        if is_remote and not is_wsl and not desktop_mode and not display_env:
            browser_env = (
                env_overrides.get("BROWSER") or os.getenv("BROWSER", "")
            ).strip()
            vscode_ipc_hook = (
                env_overrides.get("VSCODE_IPC_HOOK_CLI")
                or os.getenv("VSCODE_IPC_HOOK_CLI", "")
            ).strip()
            missing_vars: list[str] = []
            if not browser_env:
                missing_vars.append("BROWSER")
            if not vscode_ipc_hook:
                missing_vars.append("VSCODE_IPC_HOOK_CLI")

            if missing_vars:
                missing_str = ", ".join(missing_vars)
                raise InteractiveFeedbackPrerequisiteError(
                    "interactive_feedback 在当前远端/无 DISPLAY 环境不可用："
                    f"缺少环境变量 {missing_str}，无法启动本地浏览器。\n"
                    "请改用 telegram_feedback 工具作为回退反馈通道。\n"
                    "（可先调用 get_system_info 查看当前 MCP 进程环境变量。）"
                )

        with request_env_overrides(env_overrides):
            result = await launch_web_feedback_ui(project_directory, summary, timeout)

        if result:
            save_feedback_to_file(result)
        feedback_items = build_feedback_items(result)

        debug_log(f"回饋收集完成，共 {len(feedback_items)} 個項目")
        return feedback_items

    except InteractiveFeedbackPrerequisiteError:
        raise
    except TimeoutError as exc:
        if raise_on_timeout:
            raise RuntimeError(_build_interactive_feedback_timeout_message(timeout)) from exc

        error_id = ErrorHandler.log_error_with_context(
            exc,
            context={"operation": "回饋收集", "project_dir": project_directory},
            error_type=ErrorType.TIMEOUT,
        )
        user_error_msg = ErrorHandler.format_user_error(exc, include_technical=False)
        debug_log(f"回饋收集錯誤 [錯誤ID: {error_id}]: {exc!s}")
        return [TextContent(type="text", text=user_error_msg)]
    except Exception as e:
        error_id = ErrorHandler.log_error_with_context(
            e,
            context={"operation": "回饋收集", "project_dir": project_directory},
            error_type=ErrorType.SYSTEM,
        )
        user_error_msg = ErrorHandler.format_user_error(e, include_technical=False)
        debug_log(f"回饋收集錯誤 [錯誤ID: {error_id}]: {e!s}")
        return [TextContent(type="text", text=user_error_msg)]


async def _telegram_feedback_impl(project_directory: str, summary: str) -> list:
    """Collect feedback through Telegram."""
    project_directory = _normalize_project_directory(project_directory)

    try:
        debug_log("回饋模式: telegram")
        timeout = _resolve_feedback_timeout()
        result = await launch_telegram_feedback(project_directory, summary, timeout)

        if result:
            save_feedback_to_file(result)

        feedback_items = build_feedback_items(result)
        debug_log(f"Telegram 回饋收集完成，共 {len(feedback_items)} 個項目")
        return feedback_items

    except Exception as e:
        error_id = ErrorHandler.log_error_with_context(
            e,
            context={"operation": "Telegram 回饋收集", "project_dir": project_directory},
            error_type=ErrorType.SYSTEM,
        )
        user_error_msg = ErrorHandler.format_user_error(e, include_technical=False)
        debug_log(f"Telegram 回饋收集錯誤 [錯誤ID: {error_id}]: {e!s}")
        return [TextContent(type="text", text=user_error_msg)]


@mcp.tool()
async def interactive_feedback(
    project_directory: Annotated[str, Field(description="專案目錄路徑")] = ".",
    summary: Annotated[
        str, Field(description="AI 工作完成的摘要說明")
    ] = "我已完成了您請求的任務。",
) -> list:
    """Interactive feedback collection tool for LLM agents.

    USAGE RULES:
    1. During any process, task, or conversation, whether asking, replying, or completing phased tasks, you must call this tool to ask for feedback.
    2. Unless receiving termination instructions, all steps must repeatedly call this tool.
    3. Whenever user feedback is received, if the feedback content is not empty, you must call this tool again and adjust behavior based on the feedback content.
    4. Only when the user explicitly indicates "end" or "no more interaction needed" can you stop calling this tool, and the process is considered complete.
    5. You should summarize what have done, and provide project directory through args to let user know what you have done to provide feedback for next step.

    Args:
        project_directory: Project directory path for context
        summary: Summary of AI work completed for user review
        Timeout is configured via MCP_FEEDBACK_TIMEOUT environment variable (default: 600 seconds)

    Returns:
        list: List containing TextContent and MCPImage objects representing user feedback
    """
    if is_user_away():
        raise RuntimeError(_build_interactive_feedback_away_message())

    return await _interactive_feedback_impl(
        project_directory,
        summary,
        raise_on_timeout=True,
    )


async def launch_web_feedback_ui(project_dir: str, summary: str, timeout: int) -> dict:
    """
    啟動 Web UI 收集回饋，支援自訂超時時間

    Args:
        project_dir: 專案目錄路徑
        summary: AI 工作摘要
        timeout: 超時時間（秒）

    Returns:
        dict: 收集到的回饋資料
    """
    debug_log(f"啟動 Web UI 介面，超時時間: {timeout} 秒")

    try:
        # 使用新的 web 模組
        from .web import launch_web_feedback_ui as web_launch  # noqa: PLC0415

        # 傳遞 timeout 參數給 Web UI
        return await web_launch(project_dir, summary, timeout)
    except ImportError as e:
        # 使用統一錯誤處理
        error_id = ErrorHandler.log_error_with_context(
            e,
            context={"operation": "Web UI 模組導入", "module": "web"},
            error_type=ErrorType.DEPENDENCY,
        )
        user_error_msg = ErrorHandler.format_user_error(
            e, ErrorType.DEPENDENCY, include_technical=False
        )
        debug_log(f"Web UI 模組導入失敗 [錯誤ID: {error_id}]: {e}")

        return {
            "command_logs": "",
            "interactive_feedback": user_error_msg,
            "images": [],
        }


async def launch_telegram_feedback(project_dir: str, summary: str, timeout: int) -> dict:
    """
    啟動 Telegram Bot 回饋收集。

    Args:
        project_dir: 專案目錄路徑
        summary: AI 工作摘要
        timeout: 超時時間（秒）

    Returns:
        dict: 收集到的回饋資料
    """
    debug_log(f"啟動 Telegram 回饋，超時時間: {timeout} 秒")

    try:
        session = TelegramFeedbackSession.from_environment(summary, project_dir)
        client = TelegramBotClient(
            token=session.bot_token,
            api_base=session.api_base,
        )

        prompt_text = (
            "AI 工作摘要:\n"
            f"{summary}\n\n"
            "專案目錄:\n"
            f"{project_dir}\n\n"
            "請直接回覆文字與圖片。\n"
            "送出請輸入 /done"
        )
        if _http_telegram_gateway_is_running():
            request = create_pending_feedback_request(
                session.chat_id,
                project_dir,
                summary,
            )
            try:
                response = await client.send_message(session.chat_id, prompt_text)
            except Exception:
                discard_pending_feedback_request(request.request_id)
                raise

            message_id = response.get("message_id") if isinstance(response, dict) else None
            register_pending_feedback_message(request.request_id, message_id)
            return await wait_for_pending_feedback(request, timeout)

        await client.send_message(session.chat_id, prompt_text)
        return await session.collect_feedback(client, timeout)
    except ValueError as e:
        debug_log(f"Telegram 配置錯誤: {e}")
        return {
            "command_logs": "",
            "interactive_feedback": str(e),
            "images": [],
        }
    except TelegramClientError as e:
        debug_log(f"Telegram API 錯誤: {e}")
        return {
            "command_logs": "",
            "interactive_feedback": str(e),
            "images": [],
        }
    except TelegramFeedbackCancelled:
        debug_log("Telegram 回饋已取消")
        return {}


@mcp.tool()
async def telegram_feedback(
    project_directory: Annotated[str, Field(description="專案目錄路徑")] = ".",
    summary: Annotated[
        str, Field(description="AI 工作完成的摘要說明")
    ] = "我已完成了您請求的任務。",
) -> list:
    """Telegram feedback collection tool for LLM agents --- yet another feedback channel option which can 100% guarantee that humans can see your feedback..

        Call this when interactive_feedback is not available (e.g. in away mode, or when Web UI feedback times out), or you want to ensure that users will see your feedback and provide response through Telegram.
    """
    return await _telegram_feedback_impl(project_directory, summary)


@mcp.tool()
async def telegram_confirm_completion(
    project_directory: Annotated[str, Field(description="專案目錄路徑")] = ".",
    summary: Annotated[
        str, Field(description="AI 工作完成的摘要說明")
    ] = "The requested work is finished.",
) -> dict[str, object]:
    """Ask the Telegram user whether the agent may stop the current task (mark as complete)."""
    project_directory = _normalize_project_directory(project_directory)
    timeout_seconds = _resolve_feedback_timeout()

    try:
        session = TelegramFeedbackSession.from_environment(summary, project_directory)
        client = TelegramBotClient(
            token=session.bot_token,
            api_base=session.api_base,
        )
        request = create_completion_confirmation_request(
            session.chat_id,
            project_directory,
            summary,
        )
        response = await client.send_message(
            session.chat_id,
            _build_completion_confirmation_prompt(summary, project_directory),
            reply_markup=build_completion_confirmation_keyboard(request.request_id),
        )
        message_id = response.get("message_id")
        register_completion_confirmation_message(
            request.request_id,
            message_id if isinstance(message_id, int) else None,
        )
        return await wait_for_completion_confirmation(request, timeout_seconds)
    except (ValueError, TelegramClientError) as exc:
        debug_log(f"Telegram 完成確認不可用: {exc}")
        return {
            "approved": False,
            "decision": "unavailable",
            "response_text": str(exc),
        }
    except Exception as exc:
        error_id = ErrorHandler.log_error_with_context(
            exc,
            context={"operation": "Telegram 完成確認", "project_dir": project_directory},
            error_type=ErrorType.SYSTEM,
        )
        debug_log(f"Telegram 完成確認錯誤 [錯誤ID: {error_id}]: {exc!s}")
        return {
            "approved": False,
            "decision": "error",
            "response_text": str(exc),
        }


@mcp.tool()
def get_system_info() -> str:
    """
    獲取系統環境資訊

    Returns:
        str: JSON 格式的系統資訊
    """
    is_remote = is_remote_environment()
    is_wsl = is_wsl_environment()

    headers = {}
    env_overrides = {}
    try:
        from fastmcp.server.dependencies import get_http_headers  # noqa: PLC0415

        from .utils.request_env import (  # noqa: PLC0415
            extract_browser_env_overrides_from_headers,
            request_env_overrides,
        )

        headers = get_http_headers()
        env_overrides = extract_browser_env_overrides_from_headers(headers)
    except Exception as exc:
        # 僅用於調試資訊，不應影響主要 JSON 輸出
        env_overrides = {"_error": str(exc)}

    if env_overrides and "_error" not in env_overrides:
        with request_env_overrides(env_overrides):
            browser_debug_info = _collect_browser_runtime_debug_info()
    else:
        browser_debug_info = _collect_browser_runtime_debug_info()

    system_info = {
        "平台": sys.platform,
        "Python 版本": sys.version.split()[0],
        "WSL 環境": is_wsl,
        "遠端環境": is_remote,
        "介面類型": "Web UI",
        "原始碼資訊": _collect_source_debug_info(),
        "瀏覽器調試資訊": browser_debug_info,
        "請求覆寫環境變數": env_overrides if "_error" not in env_overrides else {},
        "環境變數": {
            "SSH_CONNECTION": os.getenv("SSH_CONNECTION"),
            "SSH_CLIENT": os.getenv("SSH_CLIENT"),
            "DISPLAY": os.getenv("DISPLAY"),
            "BROWSER": os.getenv("BROWSER"),
            "VSCODE_IPC_HOOK_CLI": os.getenv("VSCODE_IPC_HOOK_CLI"),
            "VSCODE_INJECTION": os.getenv("VSCODE_INJECTION"),
            "SESSIONNAME": os.getenv("SESSIONNAME"),
            "WSL_DISTRO_NAME": os.getenv("WSL_DISTRO_NAME"),
            "WSL_INTEROP": os.getenv("WSL_INTEROP"),
            "WSLENV": os.getenv("WSLENV"),
        },
    }

    return json.dumps(system_info, ensure_ascii=False, indent=2)


# ===== 主程式入口 =====
def main():
    """主要入口點，用於套件執行
    收集用戶的互動回饋，支援文字和圖片
    此工具使用 Web UI 介面收集用戶回饋，支援智能環境檢測。

    用戶可以：
    1. 執行命令來驗證結果
    2. 提供文字回饋
    3. 上傳圖片作為回饋
    4. 查看 AI 的工作摘要

    調試模式：
    - 設置環境變數 MCP_DEBUG=true 可啟用詳細調試輸出
    - 生產環境建議關閉調試模式以避免輸出干擾


    """
    # 檢查是否啟用調試模式
    debug_enabled = os.getenv("MCP_DEBUG", "").lower() in ("true", "1", "yes", "on")

    # 檢查是否啟用桌面模式
    desktop_mode = os.getenv("MCP_DESKTOP_MODE", "").lower() in (
        "true",
        "1",
        "yes",
        "on",
    )

    if debug_enabled:
        debug_log("🚀 啟動互動式回饋收集 MCP 服務器")
        debug_log(f"   服務器名稱: {SERVER_NAME}")
        debug_log(f"   版本: {__version__}")
        debug_log(f"   平台: {sys.platform}")
        debug_log(f"   編碼初始化: {'成功' if _encoding_initialized else '失敗'}")
        debug_log(f"   遠端環境: {is_remote_environment()}")
        debug_log(f"   WSL 環境: {is_wsl_environment()}")
        debug_log(f"   桌面模式: {'啟用' if desktop_mode else '禁用'}")
        debug_log("   介面類型: Web UI")
        debug_log("   等待來自 AI 助手的調用...")
        debug_log("準備啟動 MCP 伺服器...")
        debug_log("調用 mcp.run()...")

    try:
        # 使用正確的 FastMCP API
        run_mcp_server(
            transport=os.getenv("MCP_TRANSPORT", "stdio"),
            host=os.getenv("MCP_HTTP_HOST", "127.0.0.1"),
            port=int(os.getenv("MCP_HTTP_PORT", "8000")),
        )
    except KeyboardInterrupt:
        if debug_enabled:
            debug_log("收到中斷信號，正常退出")
        sys.exit(0)
    except Exception as e:
        if debug_enabled:
            debug_log(f"MCP 服務器啟動失敗: {e}")
            import traceback  # noqa: PLC0415

            debug_log(f"詳細錯誤: {traceback.format_exc()}")
        sys.exit(1)

def run_mcp_server(
    transport: str = "stdio",
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Run the FastMCP server with the requested transport."""
    if transport == "http":
        maybe_start_http_telegram_gateway()
        mcp.run(transport="http", host=host, port=port)
        return

    mcp.run()


def _telegram_gateway_env_is_configured() -> bool:
    bot_token = os.getenv("MCP_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("MCP_TELEGRAM_CHAT_ID", "").strip()
    return bool(bot_token and chat_id)


def _http_telegram_gateway_is_running() -> bool:
    thread = _http_telegram_gateway_thread
    return thread is not None and thread.is_alive()


def _run_http_telegram_gateway_with_restart(
    run_gateway_func: Any,
    *,
    sleep_func: Any = time.sleep,
    restart_delay: float = TELEGRAM_GATEWAY_RESTART_DELAY_SECONDS,
) -> None:
    """Run the HTTP transport Telegram gateway and restart it after crashes."""
    while True:
        try:
            run_gateway_func()
            return
        except Exception as exc:  # pragma: no cover - defensive logging path
            debug_log(f"HTTP transport Telegram gateway stopped: {exc}")
            debug_log(
                "HTTP transport Telegram gateway will restart "
                f"in {restart_delay:.1f}s"
            )
            sleep_func(restart_delay)


def _run_http_telegram_gateway() -> None:
    from .telegram.gateway import run_gateway  # noqa: PLC0415

    _run_http_telegram_gateway_with_restart(run_gateway)


def maybe_start_http_telegram_gateway() -> bool:
    """Start the Telegram gateway alongside HTTP transport when configured."""
    global _http_telegram_gateway_thread

    if not _telegram_gateway_env_is_configured():
        debug_log(
            "Telegram gateway auto-start skipped: missing MCP_TELEGRAM_BOT_TOKEN or MCP_TELEGRAM_CHAT_ID"
        )
        return False

    with _http_telegram_gateway_lock:
        if (
            _http_telegram_gateway_thread is not None
            and _http_telegram_gateway_thread.is_alive()
        ):
            debug_log(
                "Telegram gateway auto-start skipped: background gateway is already running"
            )
            return False

        gateway_thread = threading.Thread(
            target=_run_http_telegram_gateway,
            name="mcp-feedback-http-telegram-gateway",
            daemon=True,
        )
        gateway_thread.start()
        _http_telegram_gateway_thread = gateway_thread
        debug_log("Started Telegram gateway in the background for HTTP transport")
        return True


if __name__ == "__main__":
    main()
