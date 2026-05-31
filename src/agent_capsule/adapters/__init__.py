# SPDX-License-Identifier: Apache-2.0
"""Per-tool adapters: turn one agent's transcript into capsule specs.

Each adapter knows two tool-specific things, and nothing else:
  1. its lifecycle trigger (the hook/notify the tool fires), and
  2. how to read that tool's transcript into the six-section capsule shape.

The sealing, hashing, chaining, and verification are all shared in
``agent_capsule.core`` and are identical across tools.

  claude_code  Stop / SessionEnd hook        -> session transcript JSONL
  cursor       ~/.cursor/hooks.json stop      -> transcript JSONL + globalStorage SQLite
  codex        ~/.codex/config.toml notify    -> ~/.codex/sessions rollout JSONL
  cline        ~/Documents/Cline/Hooks/*      -> tasks/<id>/*.json
"""
