# Threat model

agent-capsule makes one precise promise: **tamper evidence**. This page states
exactly what that does and does not mean, so you can rely on it correctly.

## What it guarantees

Given a chain and the matching public key, anyone can detect, at the exact
capsule, any of the following changes made after the capsule was sealed:

- **Editing** a capsule's content (any byte of any of the six sections, the
  identity, or the link fields). The recomputed SHA3-256 no longer matches the
  stored hash.
- **Reordering** capsules. The `sequence` numbers or `previous_hash` links no
  longer line up.
- **Inserting** a forged capsule in the middle. It cannot carry a valid
  signature without the private key, and it breaks the link of the next capsule.
- **Deleting** a capsule. The chain develops a sequence gap or a broken
  `previous_hash`.
- **Forging** a capsule end to end. Without the Ed25519 private key, no attacker
  can produce a signature that verifies against the public key.

The guarantee rests on standard primitives: SHA3-256 (collision and
preimage resistance) and Ed25519 (existential unforgeability). It holds even
against someone with full write access to the chain database, as long as they do
not have the private key.

## What it does NOT guarantee

Be honest with yourself about the boundaries:

- **It is evidence, not prevention.** Anyone who can write to the database can
  still alter or delete records. They simply cannot do so *undetectably*.
  Re-verification surfaces the change; it does not stop it. Pair the chain with
  ordinary file permissions and backups if you need durability.
- **It attests to what the tool persisted, not ground truth.** A capsule records
  what the agent's transcript contains. If a tool filters or omits something from
  its own transcript (Codex can filter rollout persistence; Claude Code redacts
  extended-thinking text), the chain attests only to what was actually written.
  The adapter marks what it cannot capture (for example `thinking_redacted`)
  rather than inventing it.
- **It does not protect the private key for you.** The whole model depends on the
  key at `~/.agent-capsule/key` staying secret. If the key leaks, an attacker can
  forge a parallel chain. Treat the key like any signing key (see below).
- **It does not encrypt your data.** Capsules are plaintext SQLite. The chain
  proves integrity, not confidentiality. Capsule contents (prompts, responses,
  tool I/O) are as sensitive as the session they record.
- **It does not bind a chain to wall-clock time.** `signed_at` is the signer's
  own clock, not a trusted timestamp. If you need third-party time anchoring,
  publish chain heads to an external append-only log or timestamping service.
- **It cannot prove a session existed.** If a tool never fires its trigger (a
  hard crash, or the hook was never installed), no capsule is written. The chain
  proves the integrity of what was recorded, not that everything was recorded.

## STRIDE summary

| Threat | Posture |
|--------|---------|
| **Spoofing** | Capsules are Ed25519-signed; a chain verifies against one public key. A forged capsule fails signature verification. |
| **Tampering** | The core guarantee. SHA3-256 content hashing plus `previous_hash` linkage makes any post-hoc change detectable at the exact capsule. |
| **Repudiation** | The signed chain is non-repudiable evidence of what the agent produced, holder of the key cannot later deny it (subject to key custody). |
| **Information disclosure** | Out of scope. No encryption; capsules are local plaintext. The exported bundle carries the public key only, never the private key. |
| **Denial of service** | Adapters are fail-open: errors are logged and the process exits 0, so a sealing failure can never block or crash the agent. |
| **Elevation of privilege** | Adapters run with the user's own privileges and only read transcripts and write to `~/.agent-capsule`. They request no extra capability. |

## Key custody

- The Ed25519 private key is generated on first use at `~/.agent-capsule/key`
  with `0600` permissions and never leaves the machine.
- Only the **public** key is ever shared (it ships in the export bundle so third
  parties can verify).
- Never commit the key. The repo `.gitignore` excludes `*.db`; your key lives
  outside any repo by default.
- If the key is lost, existing chains remain verifiable by anyone who already has
  the public key, but you can no longer extend them under the same identity. If
  the key is compromised, rotate it (new key, new chains) and treat
  pre-compromise chains as suspect from the compromise point forward.

## Reporting

Found a weakness in the guarantee (a way to alter a chain that still verifies)?
That is a real vulnerability. Report it privately per [../SECURITY.md](../SECURITY.md).

## See also

- [verify-it-yourself.md](verify-it-yourself.md): check the guarantee yourself in any language.
- [wire-format.md](wire-format.md): the exact bytes the guarantee is built on.
- [../SECURITY.md](../SECURITY.md): the security policy and reporting.
