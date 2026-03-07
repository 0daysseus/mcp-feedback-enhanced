# Telegram Feedback Design

**Date:** 2026-03-07
**Status:** Approved

## Goal

Add Telegram Bot support as an extra feedback channel for `mcp-feedback-enhanced`.
The new channel must be exposed as a separate MCP tool, `telegram_feedback`, so an AI agent can explicitly fall back to Telegram when the user is away from the computer.

## Scope

### In Scope

- Add a new MCP tool: `telegram_feedback(project_directory=".", summary="...", timeout=600)`.
- Use Telegram Bot API with a fixed destination chat configured via environment variables.
- Send the work summary to the configured Telegram chat.
- Accept user replies as text and images.
- Allow multi-message accumulation within one feedback session.
- End the session on `/done`.
- Cancel the session on `/cancel`.
- Return results in the same shape expected by the existing feedback pipeline:
  - text as `TextContent`
  - images as `MCPImage`

### Out of Scope

- Automatic fallback from `interactive_feedback`.
- Replacing or changing the existing Web UI or desktop feedback flows.
- Executing shell commands from Telegram.
- Multi-user routing or per-call chat discovery.
- Webhook-based Telegram delivery.

## Approved Product Decisions

- Telegram is an extra feedback channel, not a replacement for Web or desktop.
- The integration is an independent MCP tool, not an automatic downgrade path.
- The target chat is fixed through environment configuration.
- The first version supports text and image replies only.
- Session completion is explicit:
  - `/done` submits
  - `/cancel` aborts

## Configuration

The Telegram channel is enabled only when both variables are present:

- `MCP_TELEGRAM_BOT_TOKEN`
- `MCP_TELEGRAM_CHAT_ID`

Optional:

- `MCP_TELEGRAM_API_BASE`
  Default should remain Telegram's public Bot API base URL, but a custom base is useful for tests and proxies.

If required configuration is missing, `telegram_feedback` must fail fast with a user-readable error message and no polling attempt.

## Architecture

### Tool Layer

`src/mcp_feedback_enhanced/server.py` will expose a new MCP tool:

- `telegram_feedback(...)`

This tool should mirror the current structure of `interactive_feedback`:

- validate inputs and configuration
- call a channel-specific launcher
- convert the collected result into `TextContent` plus `MCPImage`
- reuse existing helper paths where practical, especially image conversion and feedback text formatting

The existing `interactive_feedback` tool must remain behaviorally unchanged.

### Telegram Channel Layer

Add a new package:

- `src/mcp_feedback_enhanced/telegram/`

Recommended module split:

- `__init__.py`
  Public exports for the Telegram channel entrypoint
- `client.py`
  Minimal async wrapper over Telegram Bot API calls
- `session.py`
  Session orchestration, update filtering, accumulation, timeout/cancel/done handling

The design should stay narrow. There is no need for a generic multi-channel abstraction in the first iteration.

## Responsibilities

### Telegram API Client

The client should support only the operations needed for this feature:

- `sendMessage`
- `getUpdates`
- `getFile`
- file download via Bot API file endpoint

The implementation should use existing project dependencies. `aiohttp` is already available and is sufficient.

### Telegram Session

The session object should:

- capture a unique `session_id`
- record a `start_time`
- establish a safe initial update offset so old messages are ignored
- send the initial Telegram prompt message
- poll for new updates until submit, cancel, or timeout
- accumulate:
  - feedback text fragments
  - image attachments
- emit a result dict compatible with the existing server pipeline

Suggested result shape:

```python
{
    "command_logs": "",
    "interactive_feedback": "...",
    "images": [
        {
            "name": "telegram_photo_1.jpg",
            "data": b"...",
            "size": 12345,
        }
    ],
}
```

## Interaction Flow

1. AI calls `telegram_feedback(project_directory, summary, timeout)`.
2. The tool validates Telegram configuration.
3. The tool creates a Telegram feedback session.
4. The service queries the current update boundary and stores the next safe offset.
5. The service sends a Telegram message to the configured chat containing:
   - work summary
   - project directory
   - instructions:
     - send text and/or images
     - send `/done` to submit
     - send `/cancel` to cancel
6. The service long-polls `getUpdates`.
7. For each update:
   - ignore messages from other chats
   - ignore messages older than the session boundary
   - append text messages
   - append image captions to the text buffer
   - download and store photo content
8. On `/done`, the session returns the accumulated content.
9. On `/cancel`, the tool returns a cancel message.
10. On timeout, the tool returns a timeout error and optionally sends a closing Telegram notice.

## Message Rules

### Text

- Plain text messages are appended in arrival order.
- Blank text is ignored unless it is a control command.
- Multiple user messages are joined with double newlines in the final aggregated feedback text.

### Images

- Telegram photo updates should use the largest available size variant.
- Caption text should be appended to the accumulated text.
- Downloaded files should be stored in memory only for the duration of the session.
- Image metadata should be normalized to the same structure already expected by `process_images`.

### Commands

- `/done` finalizes the session.
- `/cancel` aborts the session.
- Other slash commands are ignored as normal text or rejected explicitly; the first implementation should keep this simple and document the supported commands only.

## Session Isolation

Each tool invocation must isolate its own update window.

Rules:

- Only consume updates for the configured `chat_id`.
- Ignore historical updates that existed before the session started.
- Advance the polling offset as updates are processed.
- Do not assume the Telegram chat is clean.

This avoids a common failure mode where old backlog messages are mistaken for a fresh response.

## Error Handling

Use the existing error handling framework.

Expected error classes:

- missing configuration
- API request failure
- update polling failure
- image download failure
- timeout
- user cancellation

Rules:

- Missing configuration should fail fast.
- A single image download failure should not destroy the whole session if text or other images were collected.
- Sensitive values, especially bot token, must never appear in logs or user-facing errors.
- Logging should include session id, chat id, message count, and terminal outcome.

## Security Constraints

- Accept updates only from the configured `chat_id`.
- Do not execute commands received from Telegram.
- Do not fetch arbitrary external URLs from user content.
- Download files only through Telegram's file API using the configured bot token.
- Keep token handling internal to the client module.

## Testing Strategy

### Unit Tests

Add focused unit coverage for:

- missing `MCP_TELEGRAM_BOT_TOKEN`
- missing `MCP_TELEGRAM_CHAT_ID`
- text accumulation across multiple messages
- caption inclusion
- image selection from Telegram photo sizes
- `/done` completion
- `/cancel` cancellation
- timeout behavior
- filtering of updates from other chats or earlier offsets

### Integration Tests

Add integration-style tests using a mocked Telegram API surface:

- initial message send succeeds
- polling returns text and image updates
- final result is converted into `TextContent` and `MCPImage`
- failure path returns user-readable errors without crashing MCP responses

Real Telegram network calls should not be required in test runs.

## Documentation Changes

Update project docs to include:

- new environment variables
- `telegram_feedback` tool usage
- intended fallback workflow
- limitations of the first version

At minimum, `README.md` and translated readmes should be updated after implementation is stable.

## Non-Goals For First Release

- shared abstraction across Web, desktop, and Telegram channels
- Telegram webhook deployment
- inline keyboards or interactive menus
- per-user authentication beyond fixed `chat_id`
- persistent Telegram session history

## Implementation Readiness

The approved path is intentionally incremental:

1. introduce a dedicated Telegram package
2. add a new MCP tool in the server
3. reuse existing result formatting and image conversion helpers
4. cover the behavior with isolated tests before wiring docs

That keeps the Telegram channel additive and low-risk while preserving the current Web/desktop architecture.
