#!/usr/bin/env python3
"""
MCP server runtime transport tests.
"""

from mcp_feedback_enhanced import server


def test_run_mcp_server_uses_stdio_by_default(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server.mcp, "run", fake_run)

    server.run_mcp_server()

    assert captured == {}


def test_run_mcp_server_passes_http_transport_host_and_port(monkeypatch):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server.mcp, "run", fake_run)

    server.run_mcp_server(transport="http", host="127.0.0.1", port=8123)

    assert captured == {
        "transport": "http",
        "host": "127.0.0.1",
        "port": 8123,
    }
