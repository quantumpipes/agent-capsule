# SPDX-License-Identifier: Apache-2.0
"""agent-capsule: a tamper-evident audit trail for AI coding agents.

Seals every Claude Code, Cursor, Codex, and Cline session into a SHA3-256 +
Ed25519 hashchain. One shared engine (``agent_capsule.core``), one thin adapter
per tool (``agent_capsule.adapters``), and a companion explorer that re-verifies
any chain in the browser, offline.
"""

from .core import (
    Capsule,
    CapsuleChain,
    CapsuleStorage,
    ChainVerificationResult,
    Seal,
    compute_hash,
)

__version__ = "0.2.0"

__all__ = [
    "Capsule",
    "CapsuleChain",
    "ChainVerificationResult",
    "CapsuleStorage",
    "Seal",
    "compute_hash",
]
