# Capsules for Cursor

Seals every [Cursor](https://cursor.com) agent conversation into a
tamper-evident hashchain.

## Trigger

Cursor's hooks system (Cursor 1.7+). The adapter registers a `stop` (and
`sessionEnd`) hook in `~/.cursor/hooks.json`. On fire, Cursor hands the hook the
`conversation_id`, `status`, `model`, `workspace_roots`, and a `transcript_path`.

## Install

```bash
agent-capsule install cursor
```

Merges an entry running `agent-capsule-cursor-hook` into `~/.cursor/hooks.json`
under `stop` and `sessionEnd`, without removing existing hooks.

## What it captures

Cursor's transcript JSONL is **text only** (prompts and responses, no tool
calls), so the adapter enriches from Cursor's global SQLite store
(`globalStorage/state.vscdb`, table `cursorDiskKV`): it walks the conversation's
bubbles and pulls each tool call's name, arguments, and result from
`toolFormerData` (file reads/edits, terminal commands, searches), plus per-bubble
timestamps and token counts. If the SQLite store is unavailable, it falls back to
the text-only transcript so a chain is always produced.

## Storage

`~/.agent-capsule/chains/cursor/<conversation-id>.db`

## Caveats

- The SQLite store is a hot WAL database while Cursor runs; the adapter reads a
  read-only snapshot copy to stay safe and never block the agent.
- The `stop` hook is fail-open by default. The adapter returns `{}` (pure
  observer) and never blocks the agent loop.
- `transcript_path` can be null if transcripts are disabled; the adapter then
  seals from SQLite alone, keyed by `conversation_id`.
