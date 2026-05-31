# SPDX-License-Identifier: Apache-2.0
"""Seal: hash a capsule with SHA3-256 and sign it with Ed25519.

The signature is computed over the UTF-8 bytes of the hash *hex string* (not the
raw hash bytes). This matches the in-browser verifier (`@noble/ed25519` over
`utf8(hashHex)`), so chains written here verify offline in the Capsule Explorer.

The signing key lives at ~/.claude-capsule/key (32 raw Ed25519 private bytes),
generated on first use with 0600 permissions. Anyone holding the matching public
key (shipped in the export bundle) can verify; only the key holder can sign.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .paths import KEY_PATH as DEFAULT_KEY_PATH


def compute_hash(canonical: bytes) -> str:
    """SHA3-256 of the canonical bytes, hex-encoded."""
    return hashlib.sha3_256(canonical).hexdigest()


class Seal:
    """Loads (or generates) an Ed25519 key and seals/verifies capsules with it."""

    def __init__(self, key_path: Path | None = None) -> None:
        self.key_path = key_path or DEFAULT_KEY_PATH
        self._signing_key: SigningKey | None = None
        self._verify_key: VerifyKey | None = None

    def _ensure_keys(self) -> tuple[SigningKey, VerifyKey]:
        if self._signing_key is None:
            if self.key_path.exists():
                self._signing_key = SigningKey(self.key_path.read_bytes())
            else:
                self._signing_key = SigningKey.generate()
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                old_umask = os.umask(0o077)
                try:
                    self.key_path.write_bytes(bytes(self._signing_key))
                finally:
                    os.umask(old_umask)
                self.key_path.chmod(0o600)
            self._verify_key = self._signing_key.verify_key
        assert self._verify_key is not None
        return self._signing_key, self._verify_key

    def get_public_key(self) -> str:
        """Ed25519 public key as hex (shipped in the export so anyone can verify)."""
        _, vk = self._ensure_keys()
        return vk.encode().hex()

    def get_key_fingerprint(self) -> str:
        return self.get_public_key()[:16]

    def seal(self, capsule) -> "object":
        """Fill the capsule's hash, signature, signed_at, signed_by in place."""
        sk, _ = self._ensure_keys()
        hash_value = compute_hash(capsule.canonical_bytes())
        signature = sk.sign(hash_value.encode("utf-8")).signature.hex()
        capsule.hash = hash_value
        capsule.signature = signature
        capsule.signature_pq = ""  # post-quantum tier not enabled in the standalone build
        capsule.signed_at = datetime.now(timezone.utc).isoformat()
        capsule.signed_by = self.get_key_fingerprint()
        return capsule

    def verify(self, capsule) -> bool:
        """True iff the stored hash matches the content and the signature is valid."""
        if not capsule.hash or not capsule.signature:
            return False
        if compute_hash(capsule.canonical_bytes()) != capsule.hash:
            return False
        _, vk = self._ensure_keys()
        return self._verify_with(vk, capsule.hash, capsule.signature)

    @staticmethod
    def _verify_with(vk: VerifyKey, hash_hex: str, signature_hex: str) -> bool:
        try:
            vk.verify(hash_hex.encode("utf-8"), bytes.fromhex(signature_hex))
            return True
        except (BadSignatureError, ValueError):
            return False

    @staticmethod
    def verify_with_public_key(hash_hex: str, signature_hex: str, public_key_hex: str) -> bool:
        """Verify a signature against an explicit public key (no local key needed)."""
        try:
            vk = VerifyKey(bytes.fromhex(public_key_hex))
            return Seal._verify_with(vk, hash_hex, signature_hex)
        except (ValueError, TypeError):
            return False
