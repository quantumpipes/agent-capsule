# Architecture

> One sentence: each tool's trigger hands a transcript to a thin adapter, which
> turns it into capsule specs; a shared engine seals those into a signed,
> hash-linked SQLite chain; a static explorer re-verifies any chain in the
> browser. Everything below is detail.

```
  Claude Code        Cursor            Codex             Cline
  Stop/SessionEnd    stop hook         notify            TaskComplete/Cancel
       │                │                 │                  │
       ▼                ▼                 ▼                  ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │  agent_capsule.adapters.<tool>   (one thin parser per tool)        │
 │    reads the tool's transcript -> a list of capsule specs          │
 └───────────────────────────────┬──────────────────────────────────┘
                                  ▼
 ┌──────────────────────────────────────────────────────────────────┐
 │  agent_capsule.core.sealing.seal_specs(tool, session, specs)       │
 │    spec -> six-section Capsule -> SHA3-256 -> Ed25519 -> chain     │
 └───────────────────────────────┬──────────────────────────────────┘
                                  ▼
   ~/.agent-capsule/chains/<tool>/<session>.db   (SQLite, one row per capsule)
                                  │
                                  │ agent-capsule export
                                  ▼
        JSON bundle  ──▶  Capsule Explorer (re-verifies in the browser)
```

## Packages

| Module | Responsibility |
|--------|----------------|
| `core/capsule.py` | The `Capsule`: six sections + identity + `to_dict()` + `canonical_bytes()`. The exact bytes that get hashed. |
| `core/seal.py` | `Seal`: load/generate the Ed25519 key, `compute_hash()` (SHA3-256), `seal()`, `verify()`. |
| `core/storage.py` | `CapsuleStorage`: SQLite, one row per capsule, stores the canonical bytes + seal fields as columns. |
| `core/chain.py` | `CapsuleChain`: link to head, seal, persist, and `verify()` (hash + link + optional signature). |
| `core/sealing.py` | The shared adapter contract: `seal_specs(tool, session, specs)` (spec -> capsule -> chain, idempotent). |
| `core/paths.py` | Where everything lives: `~/.agent-capsule/{key, chains/<tool>/...}`. |
| `core/export.py` | Reads the chains into the explorer's static JSON bundle, tagged by tool. |
| `adapters/<tool>.py` | One per tool: install the trigger, parse the transcript into specs, call `seal_specs`. |
| `cli.py` | `verify` / `inspect` / `list` / `install` / `uninstall` / `export`. |

## Why adapters stay thin

An adapter knows exactly two tool-specific things: the lifecycle **trigger** it
installs, and how to **read that tool's transcript** into the spec shape (a plain
dict per action). Everything else (building the six-section capsule, canonical
hashing, Ed25519 signing, chain linking, idempotent re-sealing, fail-open
logging, verification) is shared in `core`. So:

- adding a tool is writing one parser, not a new crypto stack;
- every tool produces the same kind of chain;
- one explorer verifies all of them, byte for byte.

## Design choices

**Parse the transcript, not the event.** Triggers are signals. Each adapter reads
the tool's own durable transcript (Claude Code JSONL, Cursor SQLite, Codex
rollout, Cline task JSON) as the source of truth, so the capsule reflects what
actually happened, not a thin event summary.

**Store the canonical bytes.** Storage persists the exact canonical string next to
the seal fields, and export reads it verbatim, so the writer and the browser
verifier can never disagree about bytes.

**Idempotent appends.** Each spec carries a stable `key`; a per-session checkpoint
records sealed keys. A trigger can fire many times (every turn, every stop) and
only new actions append. This is what makes Codex's per-turn notify and Cline's
per-hook invocation safe.

**Fail-open.** Adapters log to `~/.agent-capsule/hook.log` and always exit 0. An
audit tool must never be able to break the thing it audits.

**Per-tool, per-session chains.** `chains/<tool>/<session>.db` keeps tools from
colliding on a session id and lets one explorer show every agent side by side.
