# claude-capsule

**A tamper-evident audit trail for your Claude Code sessions.**

A Claude Code hook seals every conversation into a cryptographic hashchain: one
signed record (a *capsule*) per tool call and per response, each linked to the
last by hash. A companion explorer re-verifies the whole chain **in your
browser, offline**: recomputes every SHA3-256 hash and checks every Ed25519
signature, with no backend and nothing to trust.

If anyone (or any process) edits, reorders, inserts, or deletes a record after
the fact, verification breaks at the exact point of tampering.

```
Claude Code session
      │  (Stop + SessionEnd hooks fire)
      ▼
capsule_chain hook ──► ~/.claude-capsule/chains/<session>.db   (signed, linked capsules)
      │
      │  claude-capsule export
      ▼
explorer/public/data/chains/*.json ──► Capsule Explorer (re-verifies in-browser, offline)
```

- **What it captures per action:** the prompt, the visible response, the full
  tool input + result, token usage, the permission mode, and proof-of-reasoning
  (Claude Code redacts extended-thinking text, so the thinking *signatures* are
  carried forward as evidence the model reasoned, with a `thinking_redacted` flag).
- **Crypto:** SHA3-256 content hash, Ed25519 signature over the hash, hash-linked
  chain. Verifiable by anyone holding the public key.
- **Offline-first:** no network, no telemetry, no account. Your session history
  never leaves your machine. The signing key lives at `~/.claude-capsule/key`.
- **One dependency:** PyNaCl. That is the whole runtime footprint of the writer.

---

## Install with Claude Code (copy / paste)

The fastest way to install. Paste the block below into a Claude Code session and
it will install the package, wire up the hooks, and verify the install for you.

````text
Install "claude-capsule" so every one of my Claude Code sessions is sealed into a
tamper-evident, cryptographically signed hashchain. Do all of this for me:

1. Install the package (try pipx first, fall back to pip --user):
     pipx install git+https://github.com/quantumpipes/claude-capsule
   or:
     python3 -m pip install --user git+https://github.com/quantumpipes/claude-capsule
   Confirm the `claude-capsule` and `claude-capsule-hook` commands are on PATH
   (e.g. `claude-capsule --help`). If they are not, find their absolute path and
   use that absolute path in step 2.

2. Register the hook in my Claude Code settings file (~/.claude/settings.json),
   creating the file and any missing keys if needed, and WITHOUT removing or
   overwriting any hooks I already have. Add an entry that runs the command
   `claude-capsule-hook` (or its absolute path from step 1) to BOTH the "Stop"
   and "SessionEnd" hook events. The shape Claude Code expects is:

     {
       "hooks": {
         "Stop":       [ { "hooks": [ { "type": "command", "command": "claude-capsule-hook" } ] } ],
         "SessionEnd": [ { "hooks": [ { "type": "command", "command": "claude-capsule-hook" } ] } ]
       }
     }

   Merge into the existing JSON. If a "Stop"/"SessionEnd" array already exists,
   append my entry to it instead of replacing it. Validate the JSON parses
   before saving.

3. Verify the install end-to-end without waiting for a real session: pick any
   transcript under ~/.claude/projects/**/ (a *.jsonl file), then run
     claude-capsule-hook --transcript "<that file>" --session install-check --finalize
   then
     claude-capsule verify ~/.claude-capsule/chains/install-check.db --signatures
   Report the verify result to me. Then delete the install-check chain:
     rm -f ~/.claude-capsule/chains/install-check.db ~/.claude-capsule/chains/install-check.checkpoint.json

4. Tell me: (a) that hooks are registered, (b) where my chains will be written
   (~/.claude-capsule/chains/), and (c) the one command to browse them later:
     git clone https://github.com/quantumpipes/claude-capsule && cd claude-capsule/explorer && npm install && npm run export && npm run dev

Do not print my key material. The signing key at ~/.claude-capsule/key is private;
only the public key is ever shared.
````

That is all most people need. The sections below are the manual path and the
reference.

---

## Manual install

```bash
# 1. Install the writer (Python 3.11+; only dependency is PyNaCl)
pipx install git+https://github.com/quantumpipes/claude-capsule
# or: python3 -m pip install --user git+https://github.com/quantumpipes/claude-capsule

# 2. Register the hook (idempotent; merges into ~/.claude/settings.json)
curl -fsSL https://raw.githubusercontent.com/quantumpipes/claude-capsule/main/install.sh | bash
```

Or wire the hook by hand in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop":       [ { "hooks": [ { "type": "command", "command": "claude-capsule-hook" } ] } ],
    "SessionEnd": [ { "hooks": [ { "type": "command", "command": "claude-capsule-hook" } ] } ]
  }
}
```

From now on, every Claude Code session appends to a chain at
`~/.claude-capsule/chains/<session-id>.db`. The `Stop` hook appends incrementally
as the session runs; the `SessionEnd` hook does a final append and verifies the
chain.

---

## Command line

```bash
claude-capsule verify  <chain.db> [--signatures]   # recompute hashes + links (and sigs); report breaks
claude-capsule inspect <chain.db> [--seq N]        # list capsules, or print one in full
claude-capsule export  --out DIR [--db PATH] [--glob PAT]   # write the explorer's JSON bundle
```

```text
$ claude-capsule verify ~/.claude-capsule/chains/<id>.db --signatures
[OK] <id>.db: 42/42 verified (head 9c8ec07009b2d759)

$ claude-capsule inspect ~/.claude-capsule/chains/<id>.db
#  0 tool    bd7b9cc27654 add a hello function
#  1 chat    9c8ec07009b2 add a hello function
```

The hook is also runnable directly for testing:

```bash
claude-capsule-hook --transcript path/to/session.jsonl --session my-id --finalize
```

---

## The explorer

A static Astro + React site that loads the exported JSON and re-verifies every
capsule client-side (`@noble/hashes` + `@noble/ed25519`). No backend.

```bash
cd explorer
npm install
npm run export   # reads ~/.claude-capsule/chains/*.db -> public/data/chains/*.json
npm run dev      # http://localhost:4840
npm test         # recomputes SHA3-256 + Ed25519 over the real exported chains
```

It shows a chains rail, a per-capsule timeline, and a detail pane, with in-browser
re-verification and a tamper test that scrolls to the exact break point.

---

## How it works

See [`docs/architecture.md`](docs/architecture.md) for the full picture and
[`docs/wire-format.md`](docs/wire-format.md) for the exact bytes.

In short:

1. **Hooks are triggers; the transcript is truth.** On `Stop`/`SessionEnd`,
   Claude Code hands the hook the session id and transcript path. The hook parses
   the transcript JSONL rather than the event payload, so it captures the full
   picture (prompts, responses, tool I/O, usage, permission mode).
2. **One capsule per action.** Each tool call becomes a `tool` capsule; each
   assistant answer becomes a `chat` capsule; attachments become `system`
   capsules. Repeated hook fires are idempotent (a per-session checkpoint tracks
   what is already sealed).
3. **Seal + link.** Each capsule's canonical JSON is hashed with SHA3-256, the
   hash hex string is signed with Ed25519, and the capsule records the previous
   capsule's hash and a sequence number, forming the chain.
4. **Verify anywhere.** Re-derive the hash from the canonical bytes, check it
   matches, check the signature against the public key, and check the links and
   sequence. The CLI does this; the browser does this; they agree byte-for-byte.

The hook is **fail-open**: any error is logged to `~/.claude-capsule/hook.log`
and it exits 0, so it can never block or stall a Claude Code session.

---

## Storage

| Mode | How | Result |
|------|-----|--------|
| Per-session (default) | nothing to set | `~/.claude-capsule/chains/<session>.db`, one independent chain each |
| Shared | `export CLAUDE_CAPSULE_DB=~/.claude-capsule/all.db` | one file, chains grouped by session id |

---

## Security model

- The signing key (`~/.claude-capsule/key`, 32 Ed25519 private bytes, `0600`) is
  generated on first use and never leaves your machine. Only the **public** key
  is shipped in the export bundle.
- Tamper evidence, not tamper *prevention*: anyone who can write to the DB can
  rewrite history, but they cannot do so **undetectably** without your private
  key. Re-verification surfaces any edit, reorder, insert, or delete.
- For an independent third party to verify your chain, share the per-chain JSON
  plus the public key. They re-verify with the explorer or any SHA3-256 + Ed25519
  implementation.
- No network, no telemetry. Report vulnerabilities per [`SECURITY.md`](SECURITY.md).

---

## Uninstall

1. Remove the two `claude-capsule-hook` entries from `~/.claude/settings.json`.
2. `pipx uninstall claude-capsule` (or `pip uninstall claude-capsule`).
3. Optionally delete your data: `rm -rf ~/.claude-capsule` (this includes your
   signing key and all chains; it is irreversible).

---

## FAQ

**Does this capture my extended thinking?** No. Claude Code strips
extended-thinking text from the stored transcript (only a cryptographic
signature survives). The hook records those signatures as proof the model
reasoned, plus a `thinking_redacted` flag. The visible response prose is captured
in full.

**Will it slow down or break my sessions?** No. It runs on `Stop`/`SessionEnd`,
does bounded work, and is fail-open: errors are logged and the process exits 0.

**Is my data sent anywhere?** Never. Everything is local files.

**Can I verify a chain without this tool?** Yes. The format is open SHA3-256 +
Ed25519 over canonical JSON (see [`docs/wire-format.md`](docs/wire-format.md)).
The browser verifier uses only MIT-licensed `@noble/*` libraries.

---

## License

Apache License 2.0. Copyright 2026 Quantum Pipes Technologies, LLC. See
[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
