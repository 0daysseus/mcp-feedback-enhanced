#!/usr/bin/env python3
"""
MCP server runtime transport tests.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import fastmcp.server.dependencies as deps
import pytest

from mcp_feedback_enhanced import server


def test_run_mcp_server_uses_stdio_by_default(monkeypatch):
    captured = {}
    gateway_calls = {"count": 0}

    def fake_run(**kwargs):
        captured.update(kwargs)

    def fake_start_gateway():
        gateway_calls["count"] += 1
        return True

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setattr(server, "maybe_start_http_telegram_gateway", fake_start_gateway)

    server.run_mcp_server()

    assert captured == {}
    assert gateway_calls["count"] == 0


def test_run_mcp_server_passes_http_transport_host_and_port(monkeypatch):
    captured = {}
    gateway_calls = {"count": 0}

    def fake_run(**kwargs):
        captured.update(kwargs)

    def fake_start_gateway():
        gateway_calls["count"] += 1
        return True

    monkeypatch.setattr(server.mcp, "run", fake_run)
    monkeypatch.setattr(server, "maybe_start_http_telegram_gateway", fake_start_gateway)

    server.run_mcp_server(transport="http", host="127.0.0.1", port=8123)

    assert captured == {
        "transport": "http",
        "host": "127.0.0.1",
        "port": 8123,
    }
    assert gateway_calls["count"] == 1


def test_maybe_start_http_telegram_gateway_skips_when_config_missing(monkeypatch):
    monkeypatch.delenv("MCP_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MCP_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(server, "_http_telegram_gateway_thread", None, raising=False)

    def fail_thread(*args, **kwargs):
        raise AssertionError("background gateway thread should not start")

    monkeypatch.setattr(server.threading, "Thread", fail_thread)

    assert server.maybe_start_http_telegram_gateway() is False


def test_maybe_start_http_telegram_gateway_starts_daemon_thread_once(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False

        def start(self):
            self.started = True
            started.append(self)

        def is_alive(self):
            return self.started

    monkeypatch.setenv("MCP_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("MCP_TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(server, "_http_telegram_gateway_thread", None, raising=False)
    monkeypatch.setattr(server.threading, "Thread", FakeThread)

    assert server.maybe_start_http_telegram_gateway() is True
    assert server.maybe_start_http_telegram_gateway() is False
    assert len(started) == 1
    assert started[0].name == "mcp-feedback-http-telegram-gateway"
    assert started[0].daemon is True


def test_run_http_telegram_gateway_with_restart_retries_after_failure(monkeypatch):
    calls = {"count": 0}
    sleep_calls = []

    def flaky_gateway():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("Bad Gateway")
        raise KeyboardInterrupt

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(server, "debug_log", lambda message: None)

    with pytest.raises(KeyboardInterrupt):
        server._run_http_telegram_gateway_with_restart(
            flaky_gateway,
            sleep_func=fake_sleep,
            restart_delay=4,
        )

    assert calls["count"] == 2
    assert sleep_calls == [4]


def test_get_system_info_includes_source_git_metadata(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    package_root = repo_root / "src" / "mcp_feedback_enhanced"
    package_root.mkdir(parents=True)
    (repo_root / ".git").write_text("gitdir: .git/worktrees/test\n", encoding="utf-8")

    fake_server_file = package_root / "server.py"
    fake_server_file.write_text("# test\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "git"
        assert Path(kwargs["cwd"]) == repo_root
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False

        if cmd[1:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        if cmd[1:] == ["status", "--short"]:
            return SimpleNamespace(returncode=0, stdout=" M src/file.py\n", stderr="")
        if cmd[1:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="main\n", stderr="")
        raise AssertionError(f"Unexpected git command: {cmd}")

    monkeypatch.setattr(server, "__file__", str(fake_server_file))
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server, "is_remote_environment", lambda: True)
    monkeypatch.setattr(server, "is_wsl_environment", lambda: False)

    system_info = json.loads(server.get_system_info())

    source_info = system_info["原始碼資訊"]
    assert source_info["來源模式"] == "git_worktree"
    assert source_info["server_file"] == str(fake_server_file.resolve())
    assert source_info["package_root"] == str(package_root.resolve())
    assert source_info["git_repo_root"] == str(repo_root.resolve())
    assert source_info["git_commit"] == "abc123"
    assert source_info["git_branch"] == "main"
    assert source_info["git_dirty"] is True
    assert source_info["git_status_short"] == " M src/file.py"


def test_get_system_info_includes_vscode_browser_env_vars(monkeypatch):
    monkeypatch.setenv("BROWSER", "/tmp/browser-helper")
    monkeypatch.setenv("VSCODE_IPC_HOOK_CLI", "/tmp/ipc-hook")
    monkeypatch.setenv("VSCODE_INJECTION", "1")

    system_info = json.loads(server.get_system_info())

    env_vars = system_info["環境變數"]
    assert env_vars["BROWSER"] == "/tmp/browser-helper"
    assert env_vars["VSCODE_IPC_HOOK_CLI"] == "/tmp/ipc-hook"
    assert env_vars["VSCODE_INJECTION"] == "1"


def test_get_system_info_reports_request_env_overrides_from_headers(
    monkeypatch, tmp_path
):
    helper = tmp_path / "browser-helper.sh"
    helper.write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    helper.chmod(0o755)

    monkeypatch.setattr(
        deps,
        "get_http_headers",
        lambda *args, **kwargs: {
            "X-MCP-ENV-BROWSER": str(helper),
            "X-MCP-ENV-VSCODE-IPC-HOOK-CLI": "/tmp/ipc-hook",
            "X-MCP-ENV-VSCODE-INJECTION": "1",
        },
    )

    system_info = json.loads(server.get_system_info())

    assert system_info["請求覆寫環境變數"] == {
        "BROWSER": str(helper),
        "VSCODE_IPC_HOOK_CLI": "/tmp/ipc-hook",
        "VSCODE_INJECTION": "1",
    }


def test_collect_browser_runtime_debug_info_reports_helper_and_session_state(
    monkeypatch,
):
    session = SimpleNamespace(
        session_id="session-123",
        websocket=object(),
        last_heartbeat=123.45,
    )

    manager = SimpleNamespace(
        get_current_session=lambda: session,
        get_global_active_tabs_count=lambda: 2,
        _pending_session_update=True,
        last_browser_launch_attempt={
            "url": "http://127.0.0.1:8765",
            "strategy": "remote_helper",
            "helper_path": "/tmp/browser.sh",
            "success": False,
            "error": "helper failed",
        },
    )

    monkeypatch.setattr(server, "find_remote_browser_helper", lambda: "/tmp/browser.sh")

    browser_info = server._collect_browser_runtime_debug_info(manager)

    assert browser_info["remote_browser_helper"] == "/tmp/browser.sh"
    assert browser_info["webui_manager_initialized"] is True
    assert browser_info["has_current_session"] is True
    assert browser_info["current_session_id"] == "session-123"
    assert browser_info["current_session_has_websocket"] is True
    assert browser_info["current_session_last_heartbeat"] == 123.45
    assert browser_info["global_active_tabs_count"] == 2
    assert browser_info["pending_session_update"] is True
    assert browser_info["last_browser_launch_attempt"] == {
        "url": "http://127.0.0.1:8765",
        "strategy": "remote_helper",
        "helper_path": "/tmp/browser.sh",
        "success": False,
        "error": "helper failed",
    }
