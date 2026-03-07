# Telegram Command Registration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automatically ensure the Telegram bot exposes `/done` and `/cancel` whenever `telegram_feedback` is used.

**Architecture:** Extend the Telegram Bot API client with minimal command-management helpers, then add a small server-side preflight that checks the current command list and registers the required commands only when needed. Keep the behavior idempotent and covered by focused unit tests.

**Tech Stack:** Python 3.11, `aiohttp`, `pytest`, FastMCP

---

### Task 1: Add failing Telegram client tests

**Files:**
- Modify: `tests/unit/test_telegram_client.py`

**Step 1: Write the failing test**

Add tests that expect:

- `TelegramBotClient.get_commands()` to call `getMyCommands`
- `TelegramBotClient.set_commands([...])` to call `setMyCommands`

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_telegram_client.py -q`
Expected: FAIL because the client has no command helpers yet

**Step 3: Write minimal implementation**

Add the two helper methods in `src/mcp_feedback_enhanced/telegram/client.py`.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_telegram_client.py -q`
Expected: PASS

### Task 2: Add failing server tests for command preflight

**Files:**
- Modify: `tests/unit/test_server_telegram_feedback.py`

**Step 1: Write the failing test**

Add tests that expect:

- no registration when `/done` and `/cancel` already exist with expected descriptions
- registration when either required command is missing

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_server_telegram_feedback.py -q`
Expected: FAIL because the server has no command-preflight helper yet

**Step 3: Write minimal implementation**

Add a helper in `src/mcp_feedback_enhanced/server.py` that compares the live command set with the required one and registers commands only when needed.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_server_telegram_feedback.py -q`
Expected: PASS

### Task 3: Wire command registration into the Telegram flow

**Files:**
- Modify: `src/mcp_feedback_enhanced/server.py`
- Modify: `src/mcp_feedback_enhanced/telegram/client.py`

**Step 1: Integrate the helper**

Ensure `launch_telegram_feedback(...)` runs the command preflight before sending the prompt message.

**Step 2: Run focused tests**

Run: `uv run pytest tests/unit/test_telegram_client.py tests/unit/test_server_telegram_feedback.py -q`
Expected: PASS

**Step 3: Run Telegram unit suite**

Run: `uv run pytest tests/unit/test_telegram_client.py tests/unit/test_telegram_session.py tests/unit/test_server_telegram_feedback.py -q`
Expected: PASS

### Task 4: Live verification

**Files:**
- No code changes expected

**Step 1: Run live bot check**

Use the configured `.env` values to call `telegram_feedback(...)` and confirm the bot exposes `/done` and `/cancel`.

**Step 2: Verify behavior**

Confirm:

- the bot command menu includes both commands
- a real feedback round-trip still succeeds
