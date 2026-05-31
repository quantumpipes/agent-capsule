# Security policy

## What this tool guarantees

agent-capsule provides **tamper evidence**, not tamper prevention. Any process
that can write to your chain database can rewrite history, but it cannot do so
**undetectably** without your Ed25519 private key. Re-verification (CLI or
in-browser) surfaces any edit, reorder, insertion, or deletion at the exact
break point.

## Key material

- The signing key lives at `~/.agent-capsule/key` (32 raw Ed25519 private
  bytes), created on first use with `0600` permissions.
- Only the **public** key is ever shared (it is included in the export bundle so
  third parties can verify).
- Never commit `~/.agent-capsule/key` or your chain `.db` files. The repo
  `.gitignore` excludes `*.db` and `*.checkpoint.json`; your key lives outside
  the repo by default.

## Privacy

- No network calls, no telemetry, no account. Capsules are local SQLite files.
- Capsules capture prompts, responses, and full tool input/output. Treat a chain
  database, and any exported bundle, as sensitive as the session it records
  before sharing it.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository, or email the address
listed in the repository profile. Please do not open a public issue for a
suspected vulnerability until it has been addressed.
