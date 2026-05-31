# SPDX-License-Identifier: Apache-2.0
"""Tests for the Cline adapter: parse a synthetic task folder, seal it, verify
the chain, and confirm a one-byte tamper breaks verification."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from agent_capsule.adapters import cline
from agent_capsule.core.chain import CapsuleChain
from agent_capsule.core.seal import Seal
from agent_capsule.core.storage import CapsuleStorage

TASK_ID = "task-abc-123"


def _write_task(base: Path) -> Path:
    task_dir = base / "tasks" / TASK_ID
    task_dir.mkdir(parents=True, exist_ok=True)

    ui_messages = [
        {"ts": 1000, "type": "say", "say": "task", "text": "Refactor the parser"},
        {"ts": 1100, "type": "say", "say": "api_req_started",
         "text": json.dumps({"request": "...", "tokensIn": 1200, "tokensOut": 340,
                             "cacheWrites": 50, "cacheReads": 10, "cost": 0.0042})},
        {"ts": 1200, "type": "say", "say": "tool",
         "text": json.dumps({"tool": "editedExistingFile", "path": "src/parser.py",
                             "diff": "@@ -1 +1 @@\n-old\n+new"})},
        {"ts": 1300, "type": "say", "say": "command", "text": "pytest -q"},
        {"ts": 1400, "type": "say", "say": "command_output", "text": "3 passed"},
        {"ts": 1500, "type": "say", "say": "completion_result",
         "text": "Done. The parser was refactored and tests pass."},
    ]
    (task_dir / "ui_messages.json").write_text(json.dumps(ui_messages), encoding="utf-8")

    api_history = [
        {"role": "user", "content": "Refactor the parser"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Editing the file."},
            {"type": "tool_use", "id": "tu_1", "name": "write_to_file",
             "input": {"path": "src/parser.py"}},
        ]},
    ]
    (task_dir / "api_conversation_history.json").write_text(
        json.dumps(api_history), encoding="utf-8")

    metadata = {
        "files_in_context": [
            {"path": "src/parser.py", "record_state": "active",
             "record_source": "cline_edited"},
        ],
        "model_usage": [
            {"ts": 1100, "model_id": "claude-sonnet-4",
             "model_provider_id": "anthropic", "mode": "act"},
        ],
        "environment_history": [
            {"ts": 1000, "os_name": "darwin", "cline_version": "3.1.0"},
        ],
    }
    (task_dir / "task_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return task_dir


def _capsule_types(db_path: Path, tenant_id=None) -> list[str]:
    storage = CapsuleStorage(db_path)
    try:
        rows = storage.get_all_ordered(tenant_id=tenant_id)
        return [json.loads(r["canonical"]).get("type") for r in rows]
    finally:
        storage.close()


def test_cline_seal_and_verify(tmp_path, monkeypatch):
    home = tmp_path / "ac-home"
    monkeypatch.setenv("AGENT_CAPSULE_HOME", str(home))
    monkeypatch.delenv("AGENT_CAPSULE_DB", raising=False)
    # core.paths reads AGENT_CAPSULE_HOME at import time; refresh it.
    import agent_capsule.core.paths as paths
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "CHAINS_DIR", home / "chains")
    monkeypatch.setattr(paths, "KEY_PATH", home / "key")
    monkeypatch.setattr(paths, "LOG_PATH", home / "hook.log")

    gs_base = tmp_path / "globalstorage"
    task_dir = _write_task(gs_base)

    rc = cline.main(["--task-dir", str(task_dir), "--session", TASK_ID, "--finalize"])
    assert rc == 0

    db_path = home / "chains" / "cline" / f"{TASK_ID}.db"
    assert db_path.exists()

    # Chain verifies clean.
    storage = CapsuleStorage(db_path)
    try:
        chain = CapsuleChain(storage)
        result = chain.verify(seal=Seal())
    finally:
        storage.close()
    assert result.valid is True
    assert result.capsules_verified >= 3

    types = _capsule_types(db_path)
    assert "tool" in types, f"expected a tool capsule, got {types}"
    assert "chat" in types, f"expected a chat capsule, got {types}"


def test_cline_tamper_breaks_verification(tmp_path, monkeypatch):
    home = tmp_path / "ac-home"
    monkeypatch.setenv("AGENT_CAPSULE_HOME", str(home))
    monkeypatch.delenv("AGENT_CAPSULE_DB", raising=False)
    import agent_capsule.core.paths as paths
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "CHAINS_DIR", home / "chains")
    monkeypatch.setattr(paths, "KEY_PATH", home / "key")
    monkeypatch.setattr(paths, "LOG_PATH", home / "hook.log")

    gs_base = tmp_path / "globalstorage"
    task_dir = _write_task(gs_base)
    cline.main(["--task-dir", str(task_dir), "--session", TASK_ID, "--finalize"])

    db_path = home / "chains" / "cline" / f"{TASK_ID}.db"

    # Flip one byte in a stored canonical payload.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT rowid_pk, canonical FROM capsules ORDER BY sequence ASC").fetchone()
    canonical = row["canonical"]
    tampered = ("X" if canonical[0] != "X" else "Y") + canonical[1:]
    conn.execute("UPDATE capsules SET canonical = ? WHERE rowid_pk = ?",
                 (tampered, row["rowid_pk"]))
    conn.commit()
    conn.close()

    storage = CapsuleStorage(db_path)
    try:
        chain = CapsuleChain(storage)
        result = chain.verify(seal=Seal())
    finally:
        storage.close()
    assert result.valid is False


def test_build_plan_carries_usage_and_authority():
    messages = [
        {"ts": 1, "type": "say", "say": "task", "text": "do it"},
        {"ts": 2, "type": "say", "say": "api_req_started",
         "text": json.dumps({"tokensIn": 10, "tokensOut": 5, "cost": 0.01})},
        {"ts": 3, "type": "ask", "ask": "tool",
         "text": json.dumps({"tool": "newFileCreated", "path": "a.py", "content": "x"})},
    ]
    meta = {"model_usage": [{"model_id": "m1"}],
            "environment_history": [{"os_name": "darwin", "cline_version": "3.0"}]}
    plan = cline.build_plan(messages, meta)
    tool_specs = [s for s in plan if s.get("type") == "tool"]
    assert tool_specs, "expected a tool spec"
    tspec = tool_specs[0]
    assert tspec["tool"] == "newFileCreated"
    assert tspec["usage"]["input_tokens"] == 10
    assert tspec["authority_type"] == "human_approved"  # ask:"tool" => approval
    assert "wrote a.py" in tspec["side_effects"]
    assert tspec["model"] == "m1"
    assert tspec["env"]["cline_version"] == "3.0"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
