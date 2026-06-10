# SPDX-License-Identifier: Apache-2.0
"""Tests for the export bundle: the keyring, per-chain signer + dates, and the
meta-chain (chain-of-conversations) that the Explorer verifies."""

from __future__ import annotations

import hashlib
import json

from agent_capsule.core import export, keyring
from agent_capsule.core.seal import Seal
from agent_capsule.core.sealing import seal_specs

from conftest import chat_spec


def _seal_two_chains(home):
    seal_specs("claude-code", "sess-a", [chat_spec("a0"), chat_spec("a1")], finalize=True)
    seal_specs("claude-code", "sess-b", [chat_spec("b0")], finalize=True)
    return sorted((home / "chains" / "claude-code").glob("*.db"))


def test_export_bundles_keyring_dates_and_signer(ac_home):
    dbs = _seal_two_chains(ac_home)
    seal = Seal()
    out = ac_home / "bundle"
    idx = export.export_chains(dbs, out, seal.get_public_key(), seal.get_key_fingerprint())

    # The bundle's keyring includes our own signing key.
    fp = seal.get_key_fingerprint()
    assert idx["keys"][fp] == seal.get_public_key()

    assert idx["chain_count"] == 2
    for c in idx["chains"]:
        assert c["signed_by"], "each chain records its signer fingerprint(s)"
        assert c["started_at"] and c["ended_at"], "recency dates present"
        assert (out / f"{c['id']}.json").exists()
    assert (out / "index.json").exists()


def test_export_includes_registered_foreign_key(ac_home):
    foreign = "ab" * 32
    fp = keyring.register(foreign)
    assert fp == foreign[:16]

    dbs = _seal_two_chains(ac_home)
    seal = Seal()
    idx = export.export_chains(dbs, ac_home / "b", seal.get_public_key(), seal.get_key_fingerprint())

    # A signer we imported but did not create is bundled alongside our own.
    assert idx["keys"][fp] == foreign
    assert seal.get_key_fingerprint() in idx["keys"]


def test_export_emits_meta_chain(ac_home):
    dbs = _seal_two_chains(ac_home)  # finalize=True records each into the meta-chain
    seal = Seal()
    out = ac_home / "b"
    idx = export.export_chains(dbs, out, seal.get_public_key(), seal.get_key_fingerprint())

    assert idx["meta"] is not None
    assert idx["meta"]["length"] == 2
    assert idx["meta"]["all_hashes_ok"] is True

    meta_json = json.loads((out / "meta.json").read_text())
    assert meta_json["length"] == 2
    # Every meta capsule's stored hash reproduces from its canonical bytes.
    for c in meta_json["capsules"]:
        assert hashlib.sha3_256(c["canonical"].encode("utf-8")).hexdigest() == c["hash"]
    # Each seal references the conversation it commits to.
    sessions = {
        json.loads(c["canonical"])["outcome"]["result"]["session_id"]
        for c in meta_json["capsules"]
    }
    assert sessions == {"sess-a", "sess-b"}


def test_export_skips_meta_when_absent(ac_home):
    # No finalize -> no meta-chain entries -> bundle has no meta section.
    seal_specs("claude-code", "sess-c", [chat_spec("c0")], finalize=False)
    dbs = sorted((ac_home / "chains" / "claude-code").glob("*.db"))
    seal = Seal()
    idx = export.export_chains(dbs, ac_home / "b", seal.get_public_key(), seal.get_key_fingerprint())
    assert idx["meta"] is None
    assert not (ac_home / "b" / "meta.json").exists()
