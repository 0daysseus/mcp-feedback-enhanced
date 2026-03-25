"""
Request-scoped environment overrides.
====================================

When running FastMCP with HTTP transport, some environment values (e.g. VS Code
Remote browser helper vars) may need to be provided per-request via HTTP
headers because they can change between calls and are hard to fix at server
startup.

This module provides a small request-scoped override mechanism using
`contextvars`, plus a strict allowlist extractor for browser-launch variables.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar


_REQUEST_ENV_OVERRIDES: ContextVar[dict[str, str] | None] = ContextVar(
    "mcp_feedback_enhanced_request_env_overrides", default=None
)

ALLOWED_BROWSER_ENV_VARS: tuple[str, ...] = (
    "BROWSER",
    "VSCODE_IPC_HOOK_CLI",
    "VSCODE_INJECTION",
)


def get_request_env_override(name: str) -> str | None:
    """
    Get an overridden environment variable for the current request context.

    Returns `None` when no override is present.
    """
    overrides = _REQUEST_ENV_OVERRIDES.get()
    if not overrides:
        return None
    return overrides.get(name)


def get_request_env_overrides() -> dict[str, str]:
    """
    Get all request-scoped environment overrides.

    Returns an empty dict when no override is present.
    """
    overrides = _REQUEST_ENV_OVERRIDES.get()
    return dict(overrides) if overrides else {}


@contextmanager
def request_env_overrides(overrides: Mapping[str, str] | None) -> Iterator[None]:
    """
    Temporarily apply request-scoped overrides for the current execution context.
    """
    sanitized = _sanitize_env_overrides(overrides)
    token = _REQUEST_ENV_OVERRIDES.set(sanitized or None)
    try:
        yield
    finally:
        _REQUEST_ENV_OVERRIDES.reset(token)


def extract_browser_env_overrides_from_headers(
    headers: Mapping[str, str] | None,
) -> dict[str, str]:
    """
    Extract browser-related env overrides from HTTP headers.

    Supported header names for each env var (case-insensitive):
    - Recommended: `x-mcp-env-<env-name-with-dashes>`
      e.g. `x-mcp-env-vscode-ipc-hook-cli`
    - Compatibility: the raw env name, with either underscores or dashes
      e.g. `VSCODE_IPC_HOOK_CLI` or `VSCODE-IPC-HOOK-CLI`
    """
    if not headers:
        return {}

    normalized_headers = {str(k).lower(): str(v) for k, v in headers.items()}
    overrides: dict[str, str] = {}

    for env_var in ALLOWED_BROWSER_ENV_VARS:
        for header_name in _candidate_header_names_for_env(env_var):
            value = normalized_headers.get(header_name)
            if value is None:
                continue
            value = value.strip()
            if value:
                overrides[env_var] = value
                break

    return overrides


def _candidate_header_names_for_env(env_var: str) -> list[str]:
    env_lower = env_var.lower()
    env_dashed = env_lower.replace("_", "-")
    candidates = [
        f"x-mcp-env-{env_dashed}",
        f"x-mcp-env-{env_lower}",
        env_lower,
        env_dashed,
    ]
    # Preserve order but remove duplicates.
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def _sanitize_env_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    if not overrides:
        return {}

    sanitized: dict[str, str] = {}
    for key, value in overrides.items():
        if key is None:
            continue
        key_str = str(key).strip()
        if not key_str:
            continue

        if value is None:
            continue
        value_str = str(value).strip()
        if not value_str:
            continue

        sanitized[key_str] = value_str

    return sanitized

