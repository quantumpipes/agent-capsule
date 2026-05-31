# Architecture

```
┌─────────────────────┐
│  Claude Code session │
└──────────┬──────────┘
           │ Stop hook (incremental)   SessionEnd hook (final + verify)
           ▼
┌─────────────────────────────────────────────────────────────┐
│  claude_capsule.hook                                          │
│   load_records()    parse transcript JSONL                    │
│   build_plan()      transcript -> ordered capsule specs       │
│   make_capsule()    spec -> six-section Capsule               │
│   CapsuleChain.seal_and_store()  link + SHA3-256 + Ed25519    │
└──────────┬───────────────────────────────────────────────────┘
           ▼
   ~/.claude-capsule/chains/<session>.db   (SQLite; one row per capsule)
           │
           │ claude_capsule.export  (reads columns directly, no re-serialize)
           ▼
   explorer/public/data/chains/{index.json, <chain>.json}
           │
           ▼
┌─────────────────────────────────────────────────────────────┐
│  Capsule Explorer (Astro + React, static)                     │
│   data-source.ts   fetch JSON bundle                          │
│   crypto.ts        SHA3-256 + Ed25519 re-verify in-browser    │
│   Explorer.tsx     chains rail / timeline / detail / tamper   │
└─────────────────────────────────────────────────────────────┘
```

## Modules (Python writer)

| File | Responsibility |
|------|----------------|
| `capsule.py` | The `Capsule` dataclass: six sections + identity + `to_dict()` + `canonical_bytes()`. Defines the exact bytes that get hashed. |
| `seal.py` | `Seal`: load/generate the Ed25519 key (`~/.claude-capsule/key`), `compute_hash()` (SHA3-256), `seal()` (sign), `verify()` / `verify_with_public_key()`. |
| `storage.py` | `CapsuleStorage`: SQLite, one row per capsule, `(tenant_id, sequence)` unique. Stores the canonical bytes + seal fields as columns. |
| `chain.py` | `CapsuleChain`: `seal_and_store()` (link to head, seal, persist) and `verify()` (hash + link + optional signature). |
| `hook.py` | The Claude Code hook: transcript parsing, the capsule plan, idempotent appends, fail-open behavior. |
| `export.py` | Reads SQLite into the explorer's static JSON bundle. |
| `cli.py` | `verify` / `inspect` / `export` subcommands. |

## Design choices

**Parse the transcript, not the event.** Hooks are triggers. The `Stop`/
`SessionEnd` event payload carries the session id and transcript path; the
transcript JSONL is the source of truth, so the hook reads it. This captures the
full prompt, response, tool I/O, usage, and permission mode rather than a thin
event envelope.

**Store the canonical bytes.** `storage.py` persists the exact canonical string
alongside the seal fields. `export.py` reads it verbatim. Nothing re-serializes
the capsule after sealing, which removes any chance of canonical drift between
writer and verifier.

**Idempotent appends.** Each capsule spec has a deterministic `key`
(`<message-uuid>:<index>`). A per-session checkpoint file records sealed keys, so
the `Stop` hook can fire repeatedly during a session and only append new actions.

**Fail-open.** The hook logs to `~/.claude-capsule/hook.log` and always exits 0.
An audit tool must never be able to break the thing it audits.

**Per-session chains by default.** One SQLite file per session means one
independent chain each: simpler to reason about, share, and verify. A shared
single-file mode (`CLAUDE_CAPSULE_DB`) groups sessions by id as `tenant_id`.

## Why two verifiers agree

The Python `verify` and the browser `crypto.ts` implement the **same three
checks** over the **same canonical bytes** with the **same signature scheme**
(Ed25519 over `utf8(hash_hex)`). The bundle ships the canonical bytes and the
public key, so the browser needs nothing else. See
[`wire-format.md`](wire-format.md).
