# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
