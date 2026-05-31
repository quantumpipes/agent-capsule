# FAQ

Short answers to the questions people ask before and after they install
agent-capsule. For the command reference see [cli.md](cli.md); for the exact
crypto see [wire-format.md](wire-format.md).

---

### Does it slow down or break my agent?

No. The adapters are fail-open: they log to `~/.agent-capsule/hook.log` and
always exit `0`. If anything goes wrong (a missing file, a locked database, a
parse error), the hook records the problem and returns cleanly, so it can never
block, cancel, or stall an agent loop. An audit tool must never be able to break
the thing it audits. The hooks observe only; the Cursor and Cline hooks return
an empty result so the agent proceeds untouched.

The sealing work also runs on the tool's trigger (a stop or a turn-complete
notify), not in the agent's hot path, and it appends only new actions per call
(see "What if my agent crashes mid-session?").

### Is anything sent anywhere?

No. There are no network calls, no telemetry, and no account. Capsules are local
SQLite files under `~/.agent-capsule/chains`. Your signing key stays at
`~/.agent-capsule/key` and never leaves your machine; only the *public* key is
ever shared, and only when you choose to export a bundle. See
[SECURITY.md](../SECURITY.md).

### Does it capture my private or extended thinking?

It captures what the tool persists, and nothing it does not. Claude Code redacts
extended-thinking *text* in its stored transcript and keeps only a thinking
signature, so the adapter records that signature as proof the model reasoned and
marks it with a `thinking_redacted` flag. It never reconstructs or invents the
hidden text. Visible assistant responses are captured in full. Other tools
follow the same rule: each adapter seals exactly what its tool actually wrote.
See [tools/claude-code.md](tools/claude-code.md).

### Can I verify a chain without trusting this tool?

Yes. The format is open and the three checks (SHA3-256 hash, Ed25519 signature
over the hash hex string, and the previous-hash chain link) are fully specified
in [wire-format.md](wire-format.md). You can re-implement them in any language
and get the same verdict on the same chain. The companion explorer does exactly
this in your browser using audited libraries, with no backend. A
step-by-step walkthrough is in [verify-it-yourself.md](verify-it-yourself.md).

### What exactly does "tamper-evident" mean? Does it prevent tampering?

It means evidence, not prevention. Any process that can write to your chain
database can rewrite history, but it cannot do so *undetectably* without your
Ed25519 private key. Re-verification surfaces any edit, reorder, insertion, or
deletion at the exact break point: the first capsule whose recomputed hash or
chain link no longer matches. agent-capsule does not lock or encrypt your files;
it makes changes provable after the fact. The full reasoning, including what the
key protects and what it does not, is in [threat-model.md](threat-model.md).

### What if I use multiple agents?

Each tool gets its own namespace: `chains/claude-code/`, `chains/cursor/`,
`chains/codex/`, `chains/cline/`. Two tools never collide on a session id, and
one explorer shows every agent's sessions side by side, each tagged with its
tool. If you would rather have one combined chain across tools, set
`AGENT_CAPSULE_DB` to a single shared store; see [cli.md](cli.md).

### What if my agent crashes mid-session?

Sealing is idempotent. Every action carries a stable `key`, and a per-session
checkpoint records which keys are already sealed, so a re-run appends only the
new actions and never duplicates. If a final trigger does not fire (for example
Claude Code is force-quit before `SessionEnd`), the next trigger of the resumed
session still appends, and the checkpoint keeps it clean. Per-tool specifics:

- **Claude Code**: `Stop` appends incrementally; `SessionEnd` finalizes. A
  force-quit that skips `SessionEnd` is recovered by the next `Stop`.
- **Codex**: there is no session-end event, so it seals per turn and finalizes on
  `task_complete`.
- **Cline**: `TaskStart`, `TaskComplete`, and `TaskCancel` all re-seal the task
  safely because sealing is idempotent.

See the per-tool pages under [tools/](tools/).

### How do I share a chain so someone else can verify it?

Run `agent-capsule export --out DIR` and hand over the resulting bundle. It
contains the chain JSON (every capsule's verbatim canonical bytes plus its seal)
and your *public* key and fingerprint in `index.json`. The recipient opens it in
the explorer, or runs the three checks themselves with any SHA3-256 + Ed25519
implementation. You never share the private key, so they can verify but no one
can forge. Treat the bundle as sensitive as the session it records before you
share it; it contains your prompts, responses, and tool I/O.

### Is it on PyPI?

Not yet. Install it from the repository:

```console
$ pipx install git+https://github.com/quantumpipes/agent-capsule
# or
$ python3 -m pip install --user git+https://github.com/quantumpipes/agent-capsule
```

The only runtime dependency is PyNaCl.

### Does it work air-gapped or offline?

Yes. Recording, verifying, and exporting are entirely local; none of them touch
the network. The explorer is a static site that verifies client-side, so you can
serve it on an isolated machine too. Installation needs network access once
(to fetch the package), after which everything runs offline.

### What data is in a capsule?

Per action: the prompt, the visible response, the tool call (name, arguments,
result, success, duration), token usage, the permission and authority posture
(so autonomous actions are distinguishable from approved ones), and per-record
provenance (cwd, git branch, model, timestamps). The six-section layout and
every field are documented in [data-model.md](data-model.md), and the exact
hashed bytes in [wire-format.md](wire-format.md).

### How big do chains get, and where are they?

One capsule is recorded per action (each tool call, each response), one row per
capsule in a SQLite file. Large tool outputs are capped (results to about 200 KB)
so a single capsule stays bounded; prompts and visible responses are kept in
full. Chains live at:

```
~/.agent-capsule/chains/<tool>/<session>.db
```

Run `agent-capsule list` to see every chain and its capsule count. Override the
base directory with `AGENT_CAPSULE_HOME`.

### Can I delete or redact a capsule?

A chain is append-only by design. Deleting, editing, or reordering any capsule
breaks verification from that point onward, which is exactly the property the
tool provides. There is no "redact one record and keep the chain valid"
operation, because that would defeat tamper evidence. If a chain contains
something you do not want to keep, treat the whole chain database as sensitive:
do not export or share it, or remove the database file entirely (which retires
that chain rather than rewriting it). See [threat-model.md](threat-model.md).

### Which Python version do I need?

Python 3.11 or newer.

---

## See also

- [../README.md](../README.md): overview and quick start.
- [cli.md](cli.md): full command-line reference.
- [wire-format.md](wire-format.md): canonical bytes, hashing, signatures.
- [verify-it-yourself.md](verify-it-yourself.md): reproduce verification anywhere.
- [data-model.md](data-model.md): what each capsule section holds.
- [threat-model.md](threat-model.md): the trust model and its limits.
- [architecture.md](architecture.md): the shared engine and per-tool adapters.

---

Apache License 2.0. Copyright 2026 Quantum Pipes Technologies, LLC.
