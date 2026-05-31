# SPDX-License-Identifier: Apache-2.0
"""The chain: append a capsule, sealing and linking it to the previous one.

Linking rule: the genesis capsule has sequence 0 and previous_hash None; every
later capsule takes sequence = prev.sequence + 1 and previous_hash = prev.hash.
Changing any capsule changes its hash, which breaks the next capsule's
previous_hash, which the verifier detects at the exact break point.
"""

from __future__ import annotations

from dataclasses import dataclass

from .capsule import Capsule
from .seal import Seal, compute_hash
from .storage import CapsuleStorage


@dataclass
class ChainVerificationResult:
    valid: bool
    capsules_verified: int = 0
    broken_at: int | None = None
    error: str | None = None


class CapsuleChain:
    """Appends capsules to, and verifies, a chain held in storage."""

    def __init__(self, storage: CapsuleStorage) -> None:
        self.storage = storage

    def seal_and_store(
        self, capsule: Capsule, seal: Seal | None = None, tenant_id: str | None = None
    ) -> Capsule:
        seal = seal or Seal()
        latest = self.storage.get_latest(tenant_id=tenant_id)
        if latest is not None:
            capsule.previous_hash = latest["hash"]
            capsule.sequence = latest["sequence"] + 1
        else:
            capsule.previous_hash = None
            capsule.sequence = 0
        seal.seal(capsule)
        return self.storage.store(capsule, tenant_id=tenant_id)

    def verify(self, tenant_id: str | None = None, seal: Seal | None = None) -> ChainVerificationResult:
        rows = self.storage.get_all_ordered(tenant_id=tenant_id)
        if not rows:
            return ChainVerificationResult(valid=True, capsules_verified=0)
        for i, row in enumerate(rows):
            if row["sequence"] != i:
                return ChainVerificationResult(False, i, i, f"sequence gap at {i}")
            if i == 0:
                if row["previous_hash"] is not None:
                    return ChainVerificationResult(False, 0, 0, "genesis has previous_hash")
            elif row["previous_hash"] != rows[i - 1]["hash"]:
                return ChainVerificationResult(False, i, i, f"link broken at {i}")
            if compute_hash(row["canonical"].encode("utf-8")) != row["hash"]:
                return ChainVerificationResult(False, i, i, f"content hash mismatch at {i}")
            if seal is not None and not Seal.verify_with_public_key(
                row["hash"], row["signature"], seal.get_public_key()
            ):
                return ChainVerificationResult(False, i, i, f"signature invalid at {i}")
        return ChainVerificationResult(valid=True, capsules_verified=len(rows))
