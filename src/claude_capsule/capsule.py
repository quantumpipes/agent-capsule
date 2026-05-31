# SPDX-License-Identifier: Apache-2.0
"""The Capsule: one tamper-evident record of one action.

A capsule answers six questions about a single action: what triggered it, what
the context was, why it happened, who authorized it, what executed, and what the
outcome was. Each capsule is hashed (SHA3-256 over its canonical bytes), signed
(Ed25519), and linked to its predecessor by hash, forming a chain.

This is an independent, single-dependency (PyNaCl) implementation of the open
Capsule wire format. ``canonical_bytes`` defines the exact bytes that get hashed;
keep it stable or you break verification of older chains.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Capsule types. Free-form strings on the wire; these are the conventional set.
TYPE_AGENT = "agent"
TYPE_TOOL = "tool"
TYPE_CHAT = "chat"
TYPE_SYSTEM = "system"


@dataclass
class Capsule:
    """One sealed, chained record of one action.

    The six sections are plain dicts so the format stays open and forgiving:
    callers fill whatever keys make sense. The conventional keys (the ones the
    explorer renders) are documented in the README's wire-format section.
    """

    # Identity + chain linkage
    type: str = TYPE_AGENT
    domain: str = "claude-code"
    id: str = field(default_factory=lambda: str(uuid4()))
    parent_id: str | None = None
    sequence: int = 0
    previous_hash: str | None = None
    spec_version: str = "1.0"

    # The six sections
    trigger: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    reasoning: dict[str, Any] = field(default_factory=dict)
    authority: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    outcome: dict[str, Any] = field(default_factory=dict)

    # Seal (filled by Seal.seal); NOT part of the hashed content
    hash: str = ""
    signature: str = ""
    signature_pq: str = ""
    signed_at: str | None = None
    signed_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The canonical content (everything that gets hashed).

        Excludes the seal fields by design, so the hash can be computed over a
        capsule that does not yet carry its own hash.
        """
        return {
            "id": self.id,
            "type": self.type,
            "domain": self.domain,
            "parent_id": self.parent_id,
            "sequence": self.sequence,
            "previous_hash": self.previous_hash,
            "spec_version": self.spec_version,
            "trigger": self.trigger,
            "context": self.context,
            "reasoning": self.reasoning,
            "authority": self.authority,
            "execution": self.execution,
            "outcome": self.outcome,
        }

    def canonical_bytes(self) -> bytes:
        """The exact bytes that get hashed: sorted-key, tight-separator, UTF-8 JSON."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def is_sealed(self) -> bool:
        return bool(self.hash and self.signature)
