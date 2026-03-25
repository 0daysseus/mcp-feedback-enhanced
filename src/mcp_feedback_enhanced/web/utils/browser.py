#!/usr/bin/env python3
"""
瀏覽器工具函數
==============

提供瀏覽器相關的工具函數，包含 WSL 環境的特殊處理。
"""

import os
import subprocess
import webbrowser
from collections.abc import Callable
from pathlib import Path

# 導入調試功能
from ...debug import server_debug_log as debug_log
from ...utils.request_env import get_request_env_override, get_request_env_overrides


REMOTE_BROWSER_HELPER_PATTERNS = (
    ".vscode-server/cli/servers/*/server/bin/helpers/browser.sh",
    ".vscode-server-insiders/cli/servers/*/server/bin/helpers/browser.sh",
    ".cursor-server/cli/servers/*/server/bin/helpers/browser.sh",
    ".windsurf-server/cli/servers/*/server/bin/helpers/browser.sh",
)

_REMOTE_HELPER_FALLBACK_PATH_ENTRIES: tuple[str, ...] = (
    # NixOS: core utilities + shells typically live here.
    "/run/current-system/sw/bin",
    # Common Linux locations.
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def is_wsl_environment() -> bool:
    """
    檢測是否在 WSL 環境中運行

    Returns:
        bool: True 表示 WSL 環境，False 表示其他環境
    """
    try:
        # 檢查 /proc/version 文件是否包含 WSL 標識
        if os.path.exists("/proc/version"):
            with open("/proc/version") as f:
                version_info = f.read().lower()
                if "microsoft" in version_info or "wsl" in version_info:
                    return True

        # 檢查 WSL 相關環境變數
        wsl_env_vars = ["WSL_DISTRO_NAME", "WSL_INTEROP", "WSLENV"]
        for env_var in wsl_env_vars:
            if os.getenv(env_var):
                return True

        # 檢查是否存在 WSL 特有的路徑
        wsl_paths = ["/mnt/c", "/mnt/d", "/proc/sys/fs/binfmt_misc/WSLInterop"]
        for path in wsl_paths:
            if os.path.exists(path):
                return True

    except Exception:
        pass

    return False


def is_desktop_mode() -> bool:
    """
    檢測是否為桌面模式

    當設置了 MCP_DESKTOP_MODE 環境變數時，禁止開啟瀏覽器

    Returns:
        bool: True 表示桌面模式，False 表示 Web 模式
    """
    return os.environ.get("MCP_DESKTOP_MODE", "").lower() == "true"


def find_remote_browser_helper() -> str | None:
    """
    探測可用的遠端 IDE 瀏覽器 helper。

    優先使用明確指向可執行檔案的 BROWSER 環境變數，
    否則搜尋 VS Code / Cursor / Windsurf Remote Server 的 helper 腳本。
    """
    browser_env = get_request_env_override("BROWSER")
    if browser_env is None:
        browser_env = os.environ.get("BROWSER", "")
    browser_env = browser_env.strip()
    if browser_env and os.path.isfile(browser_env) and os.access(browser_env, os.X_OK):
        return browser_env

    helper_candidates: list[Path] = []
    home_dir = Path.home()

    for pattern in REMOTE_BROWSER_HELPER_PATTERNS:
        helper_candidates.extend(home_dir.glob(pattern))

    valid_helpers = [
        candidate
        for candidate in helper_candidates
        if candidate.is_file() and os.access(candidate, os.X_OK)
    ]
    if not valid_helpers:
        return None

    valid_helpers.sort(key=lambda helper: helper.stat().st_mtime, reverse=True)
    return str(valid_helpers[0])


def get_browser_launch_strategy() -> dict[str, str | None]:
    """
    Describe which browser launch path will be used in the current environment.
    """
    if is_desktop_mode():
        return {"strategy": "desktop_mode", "helper_path": None}

    if is_wsl_environment():
        return {"strategy": "wsl", "helper_path": None}

    remote_browser_helper = find_remote_browser_helper()
    if remote_browser_helper:
        return {"strategy": "remote_helper", "helper_path": remote_browser_helper}

    return {"strategy": "standard_webbrowser", "helper_path": None}

def _ensure_remote_helper_path(env: dict[str, str]) -> None:
    """
    Ensure the helper subprocess has a usable PATH.

    In some MCP runtimes (especially SSH Remote / IDE-spawned tool processes),
    PATH can be stripped down. VS Code's `browser.sh` uses `#!/usr/bin/env sh`
    and relies on `dirname`/`readlink` from PATH, so we append a small set of
    common locations when missing.
    """
    current = env.get("PATH", "").strip()
    parts = [p for p in current.split(os.pathsep) if p]

    for candidate in _REMOTE_HELPER_FALLBACK_PATH_ENTRIES:
        if candidate in parts:
            continue
        if os.path.isdir(candidate):
            parts.append(candidate)

    if parts:
        env["PATH"] = os.pathsep.join(parts)


def open_browser_via_remote_helper(url: str, helper_path: str) -> None:
    """
    透過遠端 IDE helper 在本地瀏覽器開啟 URL。
    """
    env = os.environ.copy()
    env.update(get_request_env_overrides())
    _ensure_remote_helper_path(env)

    result = subprocess.run(
        [helper_path, url],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env,
    )
    if result.returncode == 0:
        debug_log(f"成功使用遠端 IDE helper 啟動瀏覽器: {helper_path}")
        return

    raise RuntimeError(
        f"遠端 IDE helper 啟動失敗 (code={result.returncode}): {result.stderr}"
    )


def open_browser_in_wsl(url: str) -> None:
    """
    在 WSL 環境中開啟 Windows 瀏覽器

    Args:
        url: 要開啟的 URL
    """
    try:
        # 嘗試使用 cmd.exe 啟動瀏覽器
        cmd = ["cmd.exe", "/c", "start", url]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )

        if result.returncode == 0:
            debug_log(f"成功使用 cmd.exe 啟動瀏覽器: {url}")
            return
        debug_log(
            f"cmd.exe 啟動失敗，返回碼: {result.returncode}, 錯誤: {result.stderr}"
        )

    except Exception as e:
        debug_log(f"使用 cmd.exe 啟動瀏覽器失敗: {e}")

    try:
        # 嘗試使用 powershell.exe 啟動瀏覽器
        cmd = ["powershell.exe", "-c", f'Start-Process "{url}"']
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )

        if result.returncode == 0:
            debug_log(f"成功使用 powershell.exe 啟動瀏覽器: {url}")
            return
        debug_log(
            f"powershell.exe 啟動失敗，返回碼: {result.returncode}, 錯誤: {result.stderr}"
        )

    except Exception as e:
        debug_log(f"使用 powershell.exe 啟動瀏覽器失敗: {e}")

    try:
        # 最後嘗試使用 wslview（如果安裝了 wslu 套件）
        cmd = ["wslview", url]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, check=False
        )

        if result.returncode == 0:
            debug_log(f"成功使用 wslview 啟動瀏覽器: {url}")
            return
        debug_log(
            f"wslview 啟動失敗，返回碼: {result.returncode}, 錯誤: {result.stderr}"
        )

    except Exception as e:
        debug_log(f"使用 wslview 啟動瀏覽器失敗: {e}")

    # 如果所有方法都失敗，拋出異常
    raise Exception("無法在 WSL 環境中啟動 Windows 瀏覽器")


def smart_browser_open(url: str) -> None:
    """
    智能瀏覽器開啟函數，根據環境選擇最佳方式

    Args:
        url: 要開啟的 URL
    """
    # 檢查是否為桌面模式
    if is_desktop_mode():
        debug_log("檢測到桌面模式，跳過瀏覽器開啟")
        return

    if is_wsl_environment():
        debug_log("檢測到 WSL 環境，使用 WSL 專用瀏覽器啟動方式")
        open_browser_in_wsl(url)
    else:
        remote_browser_helper = find_remote_browser_helper()
        if remote_browser_helper:
            debug_log(f"檢測到遠端 IDE helper，使用 helper 啟動瀏覽器: {remote_browser_helper}")
            open_browser_via_remote_helper(url, remote_browser_helper)
            return

        debug_log("使用標準瀏覽器啟動方式")
        webbrowser.open(url)


def get_browser_opener() -> Callable[[str], None]:
    """
    獲取瀏覽器開啟函數

    Returns:
        Callable: 瀏覽器開啟函數
    """
    return smart_browser_open
