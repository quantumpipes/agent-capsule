# agent-capsule documentation

Cryptographic receipts for everything your AI coding agents do. Start at the
[project README](../README.md), then go deep here.

## Understand it

| Doc | What's inside |
|-----|---------------|
| [data-model.md](data-model.md) | **What a capsule is**: the six sections (trigger, context, reasoning, authority, execution, outcome), the seal, the chain link, with a full annotated example. |
| [wire-format.md](wire-format.md) | The exact bytes: canonical JSON, SHA3-256 hashing, the Ed25519 signature scheme, the verification algorithm, the export bundle. |
| [architecture.md](architecture.md) | How each tool's trigger and transcript become capsules: the shared engine and the thin per-tool adapters. |

## Trust it

| Doc | What's inside |
|-----|---------------|
| [verify-it-yourself.md](verify-it-yourself.md) | Re-derive the hash and check the signature with none of our code, in Python and JavaScript. |
| [threat-model.md](threat-model.md) | Exactly what tamper evidence guarantees and what it does not. STRIDE summary, key custody. |
| [../SECURITY.md](../SECURITY.md) | Security policy and how to report a vulnerability. |

## Use it

| Doc | What's inside |
|-----|---------------|
| [../INSTALL.md](../INSTALL.md) | Install the package and wire up each agent. |
| [cli.md](cli.md) | Complete command-line reference: verify, inspect, list, install, uninstall, export. |
| [faq.md](faq.md) | Frequently asked questions. |

## Per-tool guides

| Tool | Guide |
|------|-------|
| Claude Code | [tools/claude-code.md](tools/claude-code.md) |
| Cursor | [tools/cursor.md](tools/cursor.md) |
| Codex | [tools/codex.md](tools/codex.md) |
| Cline | [tools/cline.md](tools/cline.md) |

## Extend it

| Doc | What's inside |
|-----|---------------|
| [writing-an-adapter.md](writing-an-adapter.md) | Add support for a new AI coding agent: a thin parser against the shared sealing contract. |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Dev setup, conventions, how to contribute. |

## Verify in the browser

The [Capsule Explorer](https://github.com/quantumpipes/capsule-explorer) is a
separate repo: a static, offline, tool-agnostic verifier for any capsule chain.
