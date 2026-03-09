# HTTP Telegram Gateway Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Streamable HTTP server transport plus a Telegram gateway mode that can browse directories under `/home/kube`, accept `/submit` and `/resume`, force Telegram feedback, expose on-demand task status, and run Codex headlessly in the chosen working directory.

**Architecture:** Keep MCP server mode and Telegram gateway mode separate. Refactor startup so MCP server transport is configurable, then add a dedicated Telegram gateway runtime with inline directory browsing, chat-local submit and resume state, JSON event tracking for running Codex jobs, and background `codex exec` subprocess execution. Preserve existing Telegram feedback behavior for MCP mode while avoiding `interactive_feedback` in Telegram gateway mode.

**Tech Stack:** Python 3.11, FastMCP, aiohttp, pytest, Telegram Bot API, Codex CLI

---

### Task 1: Add failing transport-mode tests

**Files:**
- Modify: `tests/unit/test_cli.py` or create transport-focused CLI test file if none exists
- Modify: `tests/unit/test_server_runtime.py` or create a new server runtime test file

**Step 1: Write the failing test**

Cover:

- default server transport remains stdio
- explicit HTTP transport passes host and port through to FastMCP
- telegram gateway mode does not invoke the MCP server entrypoint

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_server_runtime.py -q`
Expected: FAIL because transport/gateway dispatch does not exist yet

**Step 3: Write minimal implementation**

Add CLI parsing and runtime dispatch for:

- `server --transport ...`
- `telegram-gateway`

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_server_runtime.py -q`
Expected: PASS

### Task 2: Add failing Telegram browser and gateway-state tests

**Files:**
- Create: `tests/unit/test_telegram_gateway.py`
- Modify: `tests/unit/test_telegram_client.py`

**Step 1: Write the failing test**

Cover:

- `/submit` enters directory-browsing state
- `/resume` enters directory-browsing state for resume mode
- browser only lists directories under `/home/kube`
- pagination / parent navigation
- selecting a directory moves session into waiting-for-prompt state
- selecting a directory from `/resume` moves into waiting-for-prompt state
- sending the next text after `/resume` launches the resume job
- `/submit`, `/resume`, and `/tasks` command registration includes the new commands

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_telegram_gateway.py tests/unit/test_telegram_client.py -q`
Expected: FAIL because resume mode and task-status actions do not exist yet

**Step 3: Write minimal implementation**

Create gateway state helpers and extend Telegram command registration support.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_telegram_gateway.py tests/unit/test_telegram_client.py -q`
Expected: PASS

### Task 3: Add failing Codex execution and status tests

**Files:**
- Modify: `tests/unit/test_telegram_gateway.py`
- Create helper module tests if needed

**Step 1: Write the failing test**

Cover:

- selected Git repo directory builds `codex exec --full-auto -C <dir> <prompt>`
- `/submit` injects mandatory `telegram_feedback` instructions into the user prompt
- `/resume` injects a fixed continuation prefix plus the user-provided resume prompt
- `/resume` builds `codex exec resume --last` only after the user sends that prompt
- non-Git directory adds `--skip-git-repo-check`
- only one active job per chat
- JSON events update latest status and usage
- `/tasks` can show the latest status and terminate the task
- completion message formatting includes usage

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_telegram_gateway.py -q`
Expected: FAIL because prompt injection, resume execution, JSON status tracking, and usage formatting do not exist yet

**Step 3: Write minimal implementation**

Add subprocess launcher, JSON event tracking, prompt injection, resume launcher, and final-result message builder.

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_telegram_gateway.py -q`
Expected: PASS

### Task 4: Wire runtime modes and preserve existing feedback behavior

**Files:**
- Modify: `src/mcp_feedback_enhanced/__main__.py`
- Modify: `src/mcp_feedback_enhanced/server.py`
- Create or modify: gateway-specific Telegram runtime module(s)

**Step 1: Integrate transport mode**

Make MCP server transport configurable without breaking current stdio behavior.

**Step 2: Integrate telegram gateway**

Add a dedicated gateway entrypoint that:

- polls Telegram
- handles callback queries
- routes `/submit`
- does not expose `interactive_feedback`

**Step 3: Run focused tests**

Run: `uv run pytest tests/unit/test_cli.py tests/unit/test_server_runtime.py tests/unit/test_telegram_gateway.py tests/unit/test_telegram_client.py -q`
Expected: PASS

### Task 5: Regression and live verification

**Files:**
- No additional code expected

**Step 1: Run full Telegram-related regression suite**

Run: `uv run pytest tests/unit/test_telegram_client.py tests/unit/test_telegram_session.py tests/unit/test_server_telegram_feedback.py tests/integration/test_telegram_feedback_integration.py tests/unit/test_telegram_gateway.py tests/unit/test_cli.py tests/unit/test_server_runtime.py -q`
Expected: PASS

**Step 2: Run live verification**

Use `.env`:

- verify Streamable HTTP server starts locally
- verify Telegram `/submit` can browse directories under `/home/kube`
- verify selecting a non-Git directory triggers Codex with `--skip-git-repo-check`
- verify `/tasks` can refresh latest running status
- verify `/resume` waits for a user prompt before resuming the latest session in the selected directory
- verify Telegram task completions include usage

### Task 6: Documentation refresh

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `README.zh-TW.md`

**Step 1: Update gateway docs**

Document:

- Streamable HTTP server startup
- `/submit`
- `/resume`
- `/tasks`
- forced `telegram_feedback` behavior for gateway-launched Codex jobs
- final usage reporting

**Step 2: Run a documentation sanity check**

Review the command examples and environment variables for consistency with the implementation.
