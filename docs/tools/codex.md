# Capsules for Codex

Seals every [OpenAI Codex CLI](https://github.com/openai/codex) session into a
tamper-evident hashchain.

## Trigger

Codex's `notify` program. The adapter sets `notify = ["agent-capsule-codex-notify"]`
in your user-level `~/.codex/config.toml`. Codex invokes it on
`agent-turn-complete` with a JSON payload carrying the `thread-id`; the adapter
resolves that thread's rollout file and seals it.

## Install

```bash
agent-capsule install codex
```

Adds the `notify` line to `~/.codex/config.toml` if absent. If a different
`notify` is already set, it does not clobber it; it prints guidance instead.

## What it captures

The rollout JSONL (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`) is the source
of truth: user messages, assistant messages, function/tool calls (shell,
`apply_patch`) with their arguments and outputs (paired by `call_id`), token
usage, the model, the approval and sandbox policy, and git context.

## Storage

`~/.agent-capsule/chains/codex/<thread-id>.db`

## Caveats

- Codex fires only `agent-turn-complete`; there is **no session-end event**. The
  adapter seals per turn and finalizes when it sees a `task_complete`. Idempotency
  keys mean each turn appends only new actions, so the chain grows cleanly.
- `notify` must be in the **user-level** config; Codex ignores it in project-local
  config.
- Default rollout persistence can be filtered. The chain attests to exactly what
  Codex wrote; the adapter seals whatever is present and never invents records.
