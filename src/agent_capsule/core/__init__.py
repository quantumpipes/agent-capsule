# SPDX-License-Identifier: Apache-2.0
"""The shared capsule engine: identical for every agent tool.

A capsule is hashed (SHA3-256), signed (Ed25519), and linked to its predecessor.
Adapters in ``agent_capsule.adapters`` turn each tool's transcript into capsule
specs; this package seals and chains them.
"""

from .capsule import Capsule
from .chain import CapsuleChain, ChainVerificationResult
from .seal import Seal, compute_hash
from .storage import CapsuleStorage

__all__ = [
    "Capsule",
    "CapsuleChain",
    "ChainVerificationResult",
    "CapsuleStorage",
    "Seal",
    "compute_hash",
]
