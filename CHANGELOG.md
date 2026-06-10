# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-06-09

The keyring + meta-chain release.

### Added

- **Multi-signer keyring.** A registry of additional public keys
  (`~/.agent-capsule/known_keys.json`, fingerprint -> public-key hex) for chains
  this machine can verify but did not sign: imported chains, rotated keys, peers.
  The export bundles the whole keyring (`keys` map in `index.json`) plus each
  chain's `signed_by`, so the Explorer verifies every signer offline, not just
  the local key. New module `agent_capsule.core.keyring`.
- **Meta-chain in the export.** `export` now writes `meta.json` (the
  chain-of-conversations) and a `meta` summary in `index.json`, so the Explorer
  can confirm the whole corpus is complete (delete or truncate any conversation
  and its recorded seal no longer matches).
- **Per-chain recency dates.** Each chain summary carries `started_at` /
  `ended_at`; chains export newest-first.
- **`scripts/import_qp_chains.py`.** Import legacy qp_capsule chains
  byte-for-byte (every hash re-verified, `data -> canonical` rename), register
  the legacy signing key in the keyring, and rebuild the meta-chain.

### Tests

- Added `test_keyring`, `test_export` (keyring, dates, signer, meta-chain), and
  `test_import_qp` (round-trip hash/link preservation, idempotency, tamper
  detection). A `conftest.ac_home` fixture isolates all storage paths.

## [0.2.0] - 2026-05-31

The multi-tool release. Renamed from claude-capsule to agent-capsule.

### Added

- Adapters for Cursor (`stop` hook plus globalStorage enrichment), Codex (per-turn
  `notify` program), and Cline (`TaskComplete` / `TaskCancel` / `TaskStart` hooks),
  alongside the existing Claude Code adapter.
- A unified `agent-capsule` CLI: `verify`, `inspect`, `list`, `install`,
  `uninstall`, and `export`, with chains grouped by tool.
- Per-tool, per-session chains at `~/.agent-capsule/chains/<tool>/<session>.db`, so
  tools never collide on a session id.

### Changed

- Extracted the shared crypto into one core engine (`agent_capsule.core`), with one
  thin adapter per tool (`agent_capsule.adapters`). Every tool now produces the same
  kind of chain.
- Moved the in-browser verifier into its own repository,
  [capsule-explorer](https://github.com/quantumpipes/capsule-explorer).
- Back-compat aliases keep the original `claude-capsule` entry points working.

## [0.1.0]

Initial release as claude-capsule. Sealed Claude Code sessions into a SHA3-256 plus
Ed25519 hashchain via the `Stop` and `SessionEnd` hooks, with CLI verification.

[Unreleased]: https://github.com/quantumpipes/agent-capsule/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/quantumpipes/agent-capsule/releases/tag/v0.2.0
[0.1.0]: https://github.com/quantumpipes/agent-capsule/releases/tag/v0.1.0
