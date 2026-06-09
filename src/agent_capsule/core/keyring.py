# SPDX-License-Identifier: Apache-2.0
"""A keyring of Ed25519 public keys, indexed by fingerprint.

A capsule's ``signed_by`` is the first 16 hex chars of the signer's public key.
To verify a signature you need the full 32-byte key. Our own key is always
known; chains imported from another signer (a rotated key, a peer, the legacy
qp_capsule key) carry a fingerprint we can only resolve if their public key was
registered here.

The registry is a plain ``{fingerprint16: public_key_hex}`` JSON file at
``~/.agent-capsule/known_keys.json``. Export bundles the whole keyring so the
Explorer can verify every signer offline, not just the local key.
"""

from __future__ import annotations

import json
from typing import Any

from .paths import KNOWN_KEYS_PATH


def _fingerprint(public_key_hex: str) -> str:
    return public_key_hex[:16]


def load() -> dict[str, str]:
    """Return the registered {fingerprint: public_key_hex} map (empty if none)."""
    try:
        data: Any = json.loads(KNOWN_KEYS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def register(public_key_hex: str, label: str | None = None) -> str:
    """Add a public key to the registry (idempotent). Returns its fingerprint."""
    public_key_hex = public_key_hex.strip().lower()
    fp = _fingerprint(public_key_hex)
    keys = load()
    keys[fp] = public_key_hex
    KNOWN_KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_KEYS_PATH.write_text(json.dumps(keys, indent=2, sort_keys=True) + "\n")
    return fp


def keyring_with(own_public_key: str) -> dict[str, str]:
    """The registry plus our own key, keyed by fingerprint. For export bundling."""
    keys = load()
    if own_public_key:
        keys[_fingerprint(own_public_key)] = own_public_key.lower()
    return keys
