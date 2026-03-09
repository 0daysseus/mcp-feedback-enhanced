# HTTP Telegram Gateway Design

**Date:** 2026-03-09
**Status:** Approved

## Goal

Add two related capabilities:

1. Support running the MCP server over Streamable HTTP in addition to stdio.
2. Add a Telegram-first gateway mode where the user can browse directories with inline buttons, choose a working directory under `/home/kube`, submit or resume a Codex headless task, inspect the latest task status from Telegram, and receive completion plus feedback messages through Telegram.

## Product Decisions

- Keep stdio as the default MCP server transport.
- Add Streamable HTTP as an explicit transport option for MCP clients that connect over HTTP.
- Introduce a dedicated Telegram gateway runtime mode instead of bolting remote-task behavior onto the existing MCP feedback loop.
- In Telegram gateway mode, do not expose the `interactive_feedback` MCP tool.
- `/submit` is interactive:
  - open directory browser
  - choose working directory
  - wait for next user text as Codex task prompt
- `/resume` is interactive:
  - open the same directory browser
  - choose working directory
  - wait for next user text as the resume prompt
- Allow any directory under `/home/kube`.
- If the selected directory is not a Git repository, execute Codex with `--skip-git-repo-check`.
- Parse `codex exec --json` output in the gateway so Telegram can query task progress on demand.
- Telegram only receives:
  - task started acknowledgement
  - task finished / failed summary
  - follow-up feedback prompt

## External References

- FastMCP supports Streamable HTTP transport with explicit host and port binding.
- Telegram Bot API supports inline keyboard buttons and callback queries for paged directory navigation.
- Codex CLI supports:
  - `codex exec`
  - `codex exec resume --last`
  - `-C/--cd`
  - `--skip-git-repo-check`
  - `--json`
  - `-o/--output-last-message`

## Runtime Modes

### MCP Server Mode

`mcp-feedback-enhanced server`

- Supports `stdio` transport
- Supports `http` transport
- Registers MCP tools
- Keeps `telegram_feedback` available as a manual feedback channel

### Telegram Gateway Mode

`mcp-feedback-enhanced telegram-gateway`

- Starts Telegram polling loop
- Registers `/done`, `/cancel`, `/submit`, `/resume`, `/tasks`
- Does not run MCP server
- Does not expose `interactive_feedback`
- Handles:
  - feedback sessions
  - submit sessions
  - resume sessions
  - Codex background jobs

## MCP HTTP Transport

Add CLI and environment configuration for transport selection:

- `server --transport stdio`
- `server --transport http --host 127.0.0.1 --port 8000`
- environment fallback:
  - `MCP_TRANSPORT`
  - `MCP_HTTP_HOST`
  - `MCP_HTTP_PORT`

When transport is `http`, run FastMCP in Streamable HTTP mode. The initial implementation can keep the default FastMCP endpoint shape and document it as `/mcp/`.

## Telegram Gateway Workflow

### 1. Command Registration

Ensure Telegram commands exist:

- `/done`
- `/cancel`
- `/submit`
- `/resume`
- `/tasks`

### 2. Directory Browser

When the user sends `/submit` or `/resume`:

- create or reset chat-local gateway session state
- set current directory to `/home/kube`
- render inline keyboard with:
  - child directories
  - `..`
  - previous page / next page
  - select current directory
  - cancel

Constraints:

- never allow traversal outside `/home/kube`
- only show directories
- hide dot-prefixed directories
- sort deterministically
- paginate to keep Telegram message size reasonable

### 3. Task Submission

After `/submit` directory selection:

- persist selected directory in session state
- ask the user to send the task text
- next non-command text message becomes the Codex prompt

After `/resume` directory selection:

- persist selected directory in session state
- ask the user to send the resume prompt
- next non-command text message becomes the resume prompt

### 4. Codex Execution

Spawn a background subprocess:

- `/submit`: `codex exec --full-auto --json -o <last_message_file> -C <selected_dir> <injected_prompt>`
- `/resume`: `codex exec resume --last --json -o <last_message_file> <injected_resume_prompt>` with subprocess `cwd=<selected_dir>`

For `/submit`, inject a fixed instruction prefix before the user task.

For `/resume`, inject the same Telegram completion requirements plus the user-provided resume prompt.

- the agent is running from the Telegram gateway
- before completing, it must call `telegram_feedback`
- it must not use `interactive_feedback`
- it must wait for `telegram_feedback` to finish before ending
- the feedback summary must describe the current work and use the selected project directory

If the directory is not a Git repository, append:

- `--skip-git-repo-check`

Execution defaults:

- sandbox: workspace-write via `--full-auto`
- one active Codex job per Telegram chat
- parse JSONL events incrementally to keep live task state:
  - latest event type / stage
  - latest agent message summary
  - latest usage snapshot when available

### 5. Result Delivery

On completion:

- send success / failure summary message
- include the final Codex result text
- include usage from the latest `turn.completed` event when available
- invite the user to continue giving feedback in Telegram

On `/tasks`:

- show current running task for the chat
- include buttons:
  - `View latest status`
  - `Terminate current task`
- `View latest status` refreshes the message with the latest tracked task state so the user can see that Codex is still progressing

## State Model

Track chat-local runtime state in memory:

- mode:
  - idle
  - browsing_directory
  - waiting_for_task_prompt
  - running_codex_job
- requested action:
  - submit
  - resume
- browser path
- browser page
- selected directory
- job metadata:
  - wrapper task handle
  - subprocess handle
  - start time
  - task kind (`submit` / `resume`)
  - original prompt text when present
  - latest status text
  - latest agent message summary
  - latest usage summary

The first version keeps this state in memory only. No persistence or recovery across process restarts.

## Security and Safety

- Restrict directory traversal to `/home/kube`
- Reject symlink escapes outside `/home/kube`
- Run Codex with workspace sandboxing by default
- Only add `--skip-git-repo-check` when necessary
- Do not expose arbitrary shell execution through Telegram
- Keep one running job per chat to avoid overlap and confusion

## Testing Strategy

### Unit Tests

- CLI transport parsing for stdio/http
- server runtime dispatch for stdio/http
- directory browser rendering and pagination
- path normalization and `/home/kube` boundary enforcement
- submit / resume session state transitions
- Codex argv construction for submit, resume, and non-Git directories
- prompt injection for mandatory `telegram_feedback`
- JSON event parsing into latest status and usage summaries
- Telegram command registration including `/submit`, `/resume`, and `/tasks`

### Integration Tests

- Telegram callback flow for browsing directories
- selecting a directory then submitting text starts Codex job
- selecting a directory from `/resume` moves the gateway into waiting-for-prompt state, and the next text starts the resume job
- `/tasks` can show the latest tracked job status
- final Telegram completion message is sent
- gateway mode does not route through `interactive_feedback`

### Live Verification

- run MCP server in HTTP transport mode locally
- confirm Telegram bot can browse directories and launch a test Codex task using `.env` credentials
- confirm `/tasks` can refresh latest job status while Codex is still running
- confirm final Telegram message includes usage
