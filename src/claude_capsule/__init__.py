# SPDX-License-Identifier: Apache-2.0
"""claude-capsule: a tamper-evident audit trail for Claude Code sessions.

A Claude Code hook seals every conversation into a SHA3-256 + Ed25519 hashchain;
a companion explorer re-verifies it in the browser, offline.
"""

from .capsule import Capsule
from .chain import CapsuleChain, ChainVerificationResult
from .seal import Seal, compute_hash
from .storage import CapsuleStorage

__version__ = "0.1.0"

__all__ = [
    "Capsule",
    "CapsuleChain",
    "ChainVerificationResult",
    "CapsuleStorage",
    "Seal",
    "compute_hash",
]
