# Capsules for Claude Code

Seals every [Claude Code](https://docs.claude.com/en/docs/claude-code) session
into a tamper-evident hashchain.

## Trigger

Claude Code's `Stop` and `SessionEnd` hooks. `Stop` appends incrementally as the
session runs; `SessionEnd` does a final append and verifies the chain. The hook
receives the session id and transcript path; the adapter parses the transcript
JSONL (the source of truth), not the event payload.

## Install

```bash
agent-capsule install claude-code
```

This merges two entries into `~/.claude/settings.json` (under `Stop` and
`SessionEnd`) that run `agent-capsule-claude-hook`, without touching hooks you
already have. You can also paste a prompt and let Claude Code install it: see
[INSTALL.md](../../INSTALL.md).

## What it captures

The full prompt, the visible response, the full tool input + result, token
usage, the permission mode (so autonomous vs. approved actions are visible), and
per-record provenance. Claude Code redacts extended-thinking text in the stored
transcript (only a signature survives), so the adapter records those thinking
signatures as proof the model reasoned, with a `thinking_redacted` flag.

## Storage

`~/.agent-capsule/chains/claude-code/<session-id>.db`

## Caveats

- Per-session chains by default. Set `AGENT_CAPSULE_DB` for a single shared store.
- If Claude Code is force-quit, `SessionEnd` may not fire; the next `Stop` of a
  resumed session still appends, and the checkpoint keeps it idempotent.
