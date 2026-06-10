# SPDX-License-Identifier: Apache-2.0
"""Tests for the public-key keyring: registering signers and bundling them so
imported or rotated keys can be verified, not just the local key."""

from __future__ import annotations

from agent_capsule.core import keyring


def test_load_is_empty_with_no_registry(ac_home):
    assert keyring.load() == {}


def test_register_is_fingerprinted_and_idempotent(ac_home):
    pub = "cd" * 32  # 64 hex chars = 32-byte Ed25519 public key
    fp = keyring.register(pub)
    assert fp == pub[:16]
    assert keyring.load() == {fp: pub}

    # Registering the same key again is a no-op, not a duplicate.
    keyring.register(pub)
    assert keyring.load() == {fp: pub}


def test_register_lowercases(ac_home):
    fp = keyring.register("ABCDEF" + "00" * 29)
    assert fp == "abcdef000000000000"[:16]
    assert all(c.islower() or c.isdigit() for c in keyring.load()[fp])


def test_keyring_with_includes_own_key_and_registered(ac_home):
    foreign = "ef" * 32
    keyring.register(foreign)
    own = "12" * 32
    ring = keyring.keyring_with(own)
    assert ring[foreign[:16]] == foreign
    assert ring[own[:16]] == own
    assert len(ring) == 2
