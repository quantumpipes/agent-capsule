# SPDX-License-Identifier: Apache-2.0
"""Where Agent Capsule keeps its data, in one place.

Everything lives under ~/.agent-capsule:

    ~/.agent-capsule/
        key                     the Ed25519 signing key (0600)
        hook.log                fail-open adapter log
        chains/<tool>/<id>.db   one SQLite chain per session, namespaced by tool

Chains are namespaced by tool ("claude-code", "cursor", "codex", "cline") so a
single explorer can show every agent's sessions side by side, and so two tools
never collide on a session id.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path(os.path.expanduser(os.environ.get("AGENT_CAPSULE_HOME", "~/.agent-capsule")))
KEY_PATH = HOME / "key"
LOG_PATH = HOME / "hook.log"
CHAINS_DIR = HOME / "chains"

# The meta-chain: one capsule per finalized conversation, recording its head
# hash and capsule count. Its head commits to every conversation ever sealed.
META_DB = HOME / "meta.db"
META_LOCK = HOME / "meta.lock"
META_CHECKPOINT = HOME / "meta.checkpoint.json"


def tool_chains_dir(tool: str) -> Path:
    """Directory holding one SQLite chain per session for a given tool."""
    d = CHAINS_DIR / tool
    d.mkdir(parents=True, exist_ok=True)
    return d


def chain_db(tool: str, session_id: str) -> Path:
    """Path to the per-session chain DB for a tool."""
    return tool_chains_dir(tool) / f"{session_id}.db"


def checkpoint_file(tool: str, session_id: str) -> Path:
    """Per-session checkpoint that tracks which capsule keys are already sealed."""
    return tool_chains_dir(tool) / f"{session_id}.checkpoint.json"
