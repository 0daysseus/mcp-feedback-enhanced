# Telegram Command Registration Design

**Date:** 2026-03-07
**Status:** Approved

## Goal

Ensure the Telegram bot exposes `/done` and `/cancel` as real slash commands, and keep that state aligned automatically whenever `telegram_feedback` is invoked.

## Product Decision

- Register commands as the bot's global default command set.
- Do the check lazily when `telegram_feedback` starts instead of requiring a separate setup step.
- Only enforce the two commands required by the current product surface:
  - `/done`
  - `/cancel`

## Approach

The Telegram client will grow two narrow Bot API helpers:

- `get_commands()` wrapping `getMyCommands`
- `set_commands(commands)` wrapping `setMyCommands`

The server launcher will call a small helper before sending the feedback prompt:

1. read the current global command list
2. compare it against the required commands
3. call `setMyCommands` only if either required command is missing or has the wrong description

This keeps the behavior idempotent and avoids rewriting the command list on every invocation.

## Scope

### In Scope

- Detect whether `/done` and `/cancel` are registered
- Register them automatically when missing
- Cover the behavior with unit tests
- Verify the final behavior against the configured live bot

### Out of Scope

- Per-chat command scopes
- Locale-specific command descriptions
- Managing commands unrelated to this feedback flow

## Error Handling

- If command inspection or registration fails, `telegram_feedback` should surface the Telegram API error instead of silently continuing.
- Sensitive values must remain excluded from logs and user-facing messages.

## Testing

- Client unit tests for `getMyCommands` and `setMyCommands`
- Server-level tests for:
  - no registration when commands are already present
  - registration when commands are missing
  - preserving idempotent behavior across repeated invocations
