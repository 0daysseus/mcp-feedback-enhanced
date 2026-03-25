#!/usr/bin/env python3
"""
CLI runtime dispatch tests.
"""

import pytest

from mcp_feedback_enhanced import __main__ as cli


def test_main_dispatches_http_server_options(monkeypatch):
    captured = {}

    def fake_run_server(transport="stdio", host=None, port=None):
        captured["transport"] = transport
        captured["host"] = host
        captured["port"] = port

    monkeypatch.setattr(cli, "run_server", fake_run_server)
    monkeypatch.setattr(
        "sys.argv",
        [
            "mcp-feedback-enhanced",
            "server",
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
        ],
    )

    cli.main()

    assert captured == {
        "transport": "http",
        "host": "0.0.0.0",
        "port": 8123,
    }


def test_main_dispatches_telegram_gateway(monkeypatch):
    called = {"gateway": False}

    def fake_run_telegram_gateway():
        called["gateway"] = True

    monkeypatch.setattr(cli, "run_telegram_gateway", fake_run_telegram_gateway)
    monkeypatch.setattr("sys.argv", ["mcp-feedback-enhanced", "telegram-gateway"])

    cli.main()

    assert called["gateway"] is True
