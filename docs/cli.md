# Command-line reference

The `agent-capsule` CLI reads, verifies, and exports the chains your agents
write. You never run it to *record* a session (the per-tool hooks do that
automatically); you run it to *inspect and prove* what was recorded.

Everything here works offline, with no account and no network calls. For the
exact bytes a chain is built from, see [wire-format.md](wire-format.md); for the
trust model, see [threat-model.md](threat-model.md).

## Subcommands at a glance

| Command | What it does |
|---------|--------------|
| `agent-capsule list` | List every chain on this machine, grouped by tool |
| `agent-capsule verify <chain.db> [--signatures]` | Recompute hashes and links (and signatures) for one chain |
| `agent-capsule inspect <chain.db> [--seq N]` | Print every capsule in a chain, or one in full |
| `agent-capsule install <tool>` | Register the capsule hook for a tool |
| `agent-capsule uninstall <tool>` | Remove the capsule hook for a tool |
| `agent-capsule export --out DIR [--db PATH] [--glob PAT]` | Write the static JSON bundle the explorer reads |

`<tool>` is one of: `claude-code`, `cursor`, `codex`, `cline`.

---

## `verify`

```
agent-capsule verify <chain.db> [--signatures]
```

Re-runs the verification algorithm over a chain: for every capsule, recompute
the `SHA3-256` hash of its canonical bytes, confirm the sequence number and
`previous_hash` link, and (with `--signatures`) check the `Ed25519` signature.
The first capsule that fails any check is the break point. This is the same
algorithm the in-browser explorer runs; see
[verify-it-yourself.md](verify-it-yourself.md) to reproduce it by hand.

**Arguments**

| Argument | Meaning |
|----------|---------|
| `<chain.db>` | Path to a chain database. `~` is expanded. |
| `--signatures` | Also verify each `Ed25519` signature against your public key. Without it, only hashes and chain links are checked. |

**Example: an intact chain**

```console
$ agent-capsule verify ~/.agent-capsule/chains/cursor/a3f1c2-checkout.db --signatures
[OK] ~/.agent-capsule/chains/cursor/a3f1c2-checkout.db: 64/64 verified (head 9c8ec07009b2d759)
```

`64/64` is "capsules verified / capsules in the chain". `head` is the first 16
hex characters of the last capsule's hash, the tip of the chain.

**Example: a tampered chain**

Edit one byte of what an agent did, then verify again:

```console
$ agent-capsule verify ~/.agent-capsule/chains/cursor/a3f1c2-checkout.db --signatures
[BROKEN] ~/.agent-capsule/chains/cursor/a3f1c2-checkout.db: 18/64 verified (broken at seq 18: content hash mismatch at 18)
```

The first 18 capsules still verify; capsule 18 was altered, so its recomputed
hash no longer matches the stored hash, and every link after it is now suspect.
That break point is the whole point: you cannot rewrite history without leaving
a mark. See [threat-model.md](threat-model.md) for exactly what this does and
does not prove.

**Exit codes**

| Code | Meaning |
|------|---------|
| `0` | Chain is valid. |
| `1` | Chain is broken (the line includes the broken sequence and reason). |

---

## `inspect`

```
agent-capsule inspect <chain.db> [--seq N]
```

Prints one line per capsule: sequence number, type, the first 12 hex chars of
the hash, and the first line of the prompt that drove the action. With `--seq N`
it prints that single capsule's full canonical JSON, so you can read every field
that was hashed.

**Arguments**

| Argument | Meaning |
|----------|---------|
| `<chain.db>` | Path to a chain database. `~` is expanded. |
| `--seq N` | Print only the capsule at sequence `N`, followed by its full JSON. |

**Example: list a whole chain**

```console
$ agent-capsule inspect ~/.agent-capsule/chains/codex/9f12ab-refactor.db
#  0 chat    4b1c0a9d8e7f add a retry wrapper around the http client
#  1 tool    7a2e1f0c3d4b apply_patch src/http.py
#  2 tool    c3d4e5f60718 shell pytest -q tests/test_http.py
#  3 chat    91a0b2c3d4e5 done; all 12 tests pass
```

**Example: one capsule in full**

```console
$ agent-capsule inspect ~/.agent-capsule/chains/codex/9f12ab-refactor.db --seq 1
#  1 tool    7a2e1f0c3d4b apply_patch src/http.py
{
  "id": "7a2e1f0c-...-uuid",
  "type": "tool",
  "domain": "codex",
  "sequence": 1,
  "previous_hash": "4b1c0a9d8e7f...",
  "trigger": { "type": "user_request", "source": "9f12ab-refactor", "request": "apply_patch src/http.py" },
  "execution": { "tool_calls": [ { "tool": "apply_patch", "arguments": { "...": "..." }, "result": "...", "success": true } ] },
  "outcome": { "status": "success", "summary": "apply_patch src/http.py", "side_effects": [ "..." ] }
}
```

What each section means is documented in [data-model.md](data-model.md).

**Exit codes**

| Code | Meaning |
|------|---------|
| `0` | Capsules printed. |
| `1` | `--seq N` was given but no capsule has that sequence number. |

---

## `list`

```
agent-capsule list
```

Lists every chain under `~/.agent-capsule/chains`, grouped by tool, with the
capsule count for each. Takes no flags. Always exits `0`, even when there are no
chains yet.

**Example**

```console
$ agent-capsule list
claude-code/
  3b9a2c-feature-auth                        128 capsules
  7c1d40-bugfix-parser                         42 capsules
codex/
  9f12ab-refactor                               4 capsules
cursor/
  a3f1c2-checkout                              64 capsules
```

When nothing has been recorded yet:

```console
$ agent-capsule list
no chains yet (/Users/you/.agent-capsule/chains does not exist)
```

---

## `install`

```
agent-capsule install <tool>
```

Registers the capsule hook for `<tool>` (one of `claude-code`, `cursor`,
`codex`, `cline`). Each install is idempotent and never clobbers hooks or config
you already have. From then on, every session of that tool appends to a chain
with no further action from you. The per-tool trigger, what it captures, and its
caveats live in [tools/claude-code.md](tools/claude-code.md),
[tools/cursor.md](tools/cursor.md), [tools/codex.md](tools/codex.md), and
[tools/cline.md](tools/cline.md).

**Example**

```console
$ agent-capsule install claude-code
Installing the claude-code capsule hook...
```

What each install writes:

| Tool | Where it registers |
|------|--------------------|
| `claude-code` | Merges `Stop` and `SessionEnd` entries into `~/.claude/settings.json` |
| `cursor` | Merges `stop` and `sessionEnd` entries into `~/.cursor/hooks.json` |
| `codex` | Adds a `notify` program to `~/.codex/config.toml` |
| `cline` | Writes shim scripts to `~/Documents/Cline/Hooks/{TaskComplete,TaskCancel,TaskStart}` |

**Exit codes**: `0` on success.

---

## `uninstall`

```
agent-capsule uninstall <tool>
```

Removes the capsule hook for `<tool>`, leaving the rest of that tool's config
untouched. Your existing chains under `~/.agent-capsule/chains` are not deleted;
only the trigger is removed, so no new capsules are recorded for that tool.

**Example**

```console
$ agent-capsule uninstall cursor
```

**Exit codes**: `0` on success.

---

## `export`

```
agent-capsule export --out DIR [--db PATH ...] [--glob PAT ...]
```

Writes the static JSON bundle the [Capsule
Explorer](https://github.com/quantumpipes/capsule-explorer) reads: an
`index.json` (including your public key and fingerprint) plus one `<chain>.json`
per chain. Each chain file ships the verbatim canonical bytes of every capsule,
so the browser verifier recomputes the same hashes you do; nothing is
re-serialized in transit. The bundle layout is specified in
[wire-format.md](wire-format.md).

With no `--db` or `--glob`, export reads every chain under
`~/.agent-capsule/chains`.

**Arguments**

| Argument | Meaning |
|----------|---------|
| `--out DIR` | Required. Output directory for `index.json` and the per-chain files. |
| `--db PATH` | An explicit chain database to include. Repeatable. |
| `--glob PAT` | A glob matching chain databases to include. Repeatable. |

**Example: export everything**

```console
$ agent-capsule export --out /tmp/chains
$ ls /tmp/chains
index.json  3b9a2c-feature-auth.json  a3f1c2-checkout.json  9f12ab-refactor.json
```

**Example: export selected chains**

```console
$ agent-capsule export --out /tmp/one \
    --db ~/.agent-capsule/chains/cursor/a3f1c2-checkout.db \
    --glob '~/.agent-capsule/chains/codex/*.db'
```

Then point the explorer at the bundle (it lives in its own repo):

```console
$ git clone https://github.com/quantumpipes/capsule-explorer
$ cd capsule-explorer && npm install && npm run export && npm run dev   # http://localhost:4840
```

**Exit codes**: `0` on success.

---

## Per-tool hook entry points

Installing a tool wires its trigger to call one of these console scripts. You do
not run them by hand; the agent's hook or notify program runs them, feeds them a
JSON payload, and they seal whatever the tool just wrote. They are listed here so
you can recognize them in your tool's config.

| Console script | Called by |
|----------------|-----------|
| `agent-capsule-claude-hook` | Claude Code `Stop` / `SessionEnd` hooks |
| `agent-capsule-cursor-hook` | Cursor `stop` / `sessionEnd` hooks |
| `agent-capsule-codex-notify` | Codex `notify` program (fires per turn) |
| `agent-capsule-cline-hook` | Cline `TaskComplete` / `TaskCancel` / `TaskStart` shims |

Two back-compat aliases ship from the original `claude-capsule` release:
`claude-capsule` (same as `agent-capsule`) and `claude-capsule-hook` (same as
`agent-capsule-claude-hook`). New setups should use the `agent-capsule` names.

---

## Environment variables

| Variable | Effect |
|----------|--------|
| `AGENT_CAPSULE_HOME` | Override the base directory (default `~/.agent-capsule`). Moves the key, log, and all chains together. |
| `AGENT_CAPSULE_DB` | Switch from per-session chains to a single shared store at the given path. Every tool then appends to one chain database. |

Set them in your shell profile so both the hooks and the CLI agree:

```console
$ export AGENT_CAPSULE_HOME="/secure/vol/agent-capsule"
$ agent-capsule list
```

---

## Storage layout

```
~/.agent-capsule/                      (or $AGENT_CAPSULE_HOME)
  key                                  Ed25519 signing key, 0600, local only
  hook.log                             fail-open adapter log
  chains/
    claude-code/
      <session-id>.db                  one SQLite chain per session
      <session-id>.checkpoint.json     sealed-key checkpoint (idempotent appends)
    cursor/
      <conversation-id>.db
    codex/
      <thread-id>.db
    cline/
      <task-id>.db
```

Chains are namespaced by tool so two tools never collide on a session id, and so
one explorer can show every agent's sessions side by side. Only the *public* key
is ever shared (it travels in the export bundle); the private key at
`~/.agent-capsule/key` never leaves your machine. See [SECURITY.md](../SECURITY.md)
for key handling and [architecture.md](architecture.md) for how the pieces fit.

---

## See also

- [../README.md](../README.md): the overview and quick start.
- [wire-format.md](wire-format.md): the exact canonical bytes, hashing, and signature scheme.
- [verify-it-yourself.md](verify-it-yourself.md): reproduce verification in any language.
- [data-model.md](data-model.md): what each capsule section holds.
- [threat-model.md](threat-model.md): what tamper-evidence guarantees.
- [faq.md](faq.md): common questions.

---

Apache License 2.0. Copyright 2026 Quantum Pipes Technologies, LLC.
