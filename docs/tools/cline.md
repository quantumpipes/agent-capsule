# Capsules for Cline

Seals every [Cline](https://github.com/cline/cline) task into a tamper-evident
hashchain.

## Trigger

Cline's hooks system (Cline 3.36+). The adapter installs thin executable shims at
`~/Documents/Cline/Hooks/{TaskComplete,TaskCancel,TaskStart}`. Cline runs them
with a JSON payload on stdin (including the `taskId`); each shim execs
`agent-capsule-cline-hook`, which seals that task.

## Install

```bash
agent-capsule install cline
```

Writes the three shim scripts (and `.ps1` variants for Windows), `chmod 0755`,
idempotently. The hook returns `{}` (observe only; it never cancels a task).

## What it captures

Cline's task folder is the source of truth
(`globalStorage/saoudrizwan.claude-dev/tasks/<taskId>/`). The adapter reads
`ui_messages.json` (the durable, timestamped spine) for prompts, responses,
reasoning, file tool calls (`editedExistingFile`, `newFileCreated`, `readFile`,
...), command executions and their output, browser and MCP actions, and per-call
token/cost from `api_req_started`. Model and environment come from
`task_metadata.json`.

## Storage

`~/.agent-capsule/chains/cline/<task-id>.db`

## Caveats

- Cline rewrites its task files atomically on every change, so ordering comes
  from the in-file `ts` timestamps, not filesystem events.
- Cline can truncate early messages for context management
  (`conversationHistoryDeletedRange`); the spine (`ui_messages.json`) is sealed
  so the record survives that.
- The hook runs the same sealer for every hook type; because sealing is
  idempotent, `TaskStart`/`TaskComplete`/`TaskCancel` all safely re-seal the task.
- The task folder also lives under Cursor / VSCodium / Code - Insiders if you run
  Cline there; the adapter resolves the right base automatically.
