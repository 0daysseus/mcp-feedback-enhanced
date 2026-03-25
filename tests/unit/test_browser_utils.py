#!/usr/bin/env python3
"""
Browser utility tests.
"""

from types import SimpleNamespace

from mcp_feedback_enhanced.web.utils import browser
from mcp_feedback_enhanced.utils.request_env import request_env_overrides


def test_smart_browser_open_uses_remote_helper_when_browser_env_missing(
    monkeypatch, tmp_path
):
    helper = (
        tmp_path
        / ".vscode-server"
        / "cli"
        / "servers"
        / "Stable-test"
        / "server"
        / "bin"
        / "helpers"
        / "browser.sh"
    )
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    helper.chmod(0o755)

    subprocess_calls = []
    webbrowser_calls = []

    def fake_run(cmd, **kwargs):
        subprocess_calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stderr="")

    def fake_webbrowser_open(url):
        webbrowser_calls.append(url)
        return True

    monkeypatch.setattr(browser, "is_desktop_mode", lambda: False)
    monkeypatch.setattr(browser, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(browser.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    monkeypatch.setattr(browser.webbrowser, "open", fake_webbrowser_open)

    browser.smart_browser_open("http://127.0.0.1:12345")

    assert len(subprocess_calls) == 1
    cmd, kwargs = subprocess_calls[0]
    assert cmd == [str(helper), "http://127.0.0.1:12345"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 10
    assert kwargs["check"] is False
    assert isinstance(kwargs.get("env"), dict)
    assert kwargs["env"].get("PATH")
    assert webbrowser_calls == []


def test_find_remote_browser_helper_prefers_request_override_browser_path(
    monkeypatch, tmp_path
):
    helper = tmp_path / "browser-helper.sh"
    helper.write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    helper.chmod(0o755)

    monkeypatch.setattr(browser.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("BROWSER", raising=False)

    with request_env_overrides({"BROWSER": str(helper)}):
        assert browser.find_remote_browser_helper() == str(helper)


def test_smart_browser_open_passes_request_env_overrides_to_helper_process(
    monkeypatch, tmp_path
):
    helper = (
        tmp_path
        / ".vscode-server"
        / "cli"
        / "servers"
        / "Stable-test"
        / "server"
        / "bin"
        / "helpers"
        / "browser.sh"
    )
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/usr/bin/env sh\n", encoding="utf-8")
    helper.chmod(0o755)

    subprocess_calls = []

    def fake_run(cmd, **kwargs):
        subprocess_calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(browser, "is_desktop_mode", lambda: False)
    monkeypatch.setattr(browser, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(browser.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    monkeypatch.setattr(browser.webbrowser, "open", lambda *_: True)

    with request_env_overrides(
        {"VSCODE_IPC_HOOK_CLI": "/tmp/ipc-hook", "VSCODE_INJECTION": "1"}
    ):
        browser.smart_browser_open("http://127.0.0.1:12345")

    assert len(subprocess_calls) == 1
    cmd, kwargs = subprocess_calls[0]
    assert cmd == [str(helper), "http://127.0.0.1:12345"]
    assert kwargs["env"]["VSCODE_IPC_HOOK_CLI"] == "/tmp/ipc-hook"
    assert kwargs["env"]["VSCODE_INJECTION"] == "1"


def test_smart_browser_open_falls_back_to_webbrowser_without_remote_helper(
    monkeypatch, tmp_path
):
    webbrowser_calls = []

    def fake_webbrowser_open(url):
        webbrowser_calls.append(url)
        return True

    def fail_if_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called without helper")

    monkeypatch.setattr(browser, "is_desktop_mode", lambda: False)
    monkeypatch.setattr(browser, "is_wsl_environment", lambda: False)
    monkeypatch.setattr(browser.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(browser.subprocess, "run", fail_if_run)
    monkeypatch.setattr(browser.webbrowser, "open", fake_webbrowser_open)

    browser.smart_browser_open("http://127.0.0.1:12345")

    assert webbrowser_calls == ["http://127.0.0.1:12345"]
