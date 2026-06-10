# SPDX-License-Identifier: Apache-2.0
"""Shared test fixtures.

``ac_home`` redirects every agent-capsule storage path into a temp directory so
tests never read or write the real ``~/.agent-capsule``. Several modules bind
path constants at import time (``seal.DEFAULT_KEY_PATH``, ``export.META_DB``,
``meta.META_DB``, ``keyring.KNOWN_KEYS_PATH``), so we patch those names too, not
just the ``paths`` module.
"""

from __future__ import annotations

import pytest

from agent_capsule.core import export, keyring, meta, paths, seal


@pytest.fixture
def ac_home(tmp_path, monkeypatch):
    home = tmp_path / "ac-home"
    (home / "chains").mkdir(parents=True)
    monkeypatch.setenv("AGENT_CAPSULE_HOME", str(home))
    monkeypatch.delenv("AGENT_CAPSULE_DB", raising=False)

    # paths module globals (its helper functions read these dynamically)
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "CHAINS_DIR", home / "chains")
    monkeypatch.setattr(paths, "KEY_PATH", home / "key")
    monkeypatch.setattr(paths, "LOG_PATH", home / "hook.log")
    monkeypatch.setattr(paths, "META_DB", home / "meta.db")
    monkeypatch.setattr(paths, "META_CHECKPOINT", home / "meta.checkpoint.json")
    monkeypatch.setattr(paths, "META_LOCK", home / "meta.lock")
    monkeypatch.setattr(paths, "KNOWN_KEYS_PATH", home / "known_keys.json")

    # names bound directly in other modules at import time
    monkeypatch.setattr(seal, "DEFAULT_KEY_PATH", home / "key")
    monkeypatch.setattr(export, "CHAINS_DIR", home / "chains")
    monkeypatch.setattr(export, "META_DB", home / "meta.db")
    monkeypatch.setattr(meta, "HOME", home)
    monkeypatch.setattr(meta, "META_DB", home / "meta.db")
    monkeypatch.setattr(meta, "META_CHECKPOINT", home / "meta.checkpoint.json")
    monkeypatch.setattr(meta, "META_LOCK", home / "meta.lock")
    monkeypatch.setattr(keyring, "KNOWN_KEYS_PATH", home / "known_keys.json")
    return home


def chat_spec(key: str) -> dict:
    """A minimal sealable spec for one chat capsule."""
    return {
        "key": key,
        "type": "chat",
        "prompt": f"prompt {key}",
        "summary": f"summary {key}",
        "response": "ok",
        "success": True,
    }
