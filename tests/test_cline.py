# SPDX-License-Identifier: Apache-2.0
"""Tests for the Cline adapter: parse a synthetic task folder, seal it, verify
the chain, and confirm a one-byte tamper breaks verification."""

from __future__ import annotations

import json
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

    # A multi-line unified diff so +/- counting has something to count.
    EDIT_DIFF = (
        "@@ -1,3 +1,3 @@\n"
        " import os\n"
        "-old_line_one\n"
        "-old_line_two\n"
        "+new_line_one\n"
        "+new_line_two\n"
        "+new_line_three\n"
    )
    # A base64 data-URI screenshot that must NOT survive into the capsule.
    SCREENSHOT_B64 = "data:image/png;base64," + ("A" * 4096)

    ui_messages = [
        {"ts": 1000, "type": "say", "say": "task", "text": "Refactor the parser"},
        {"ts": 1100, "type": "say", "say": "api_req_started",
         "text": json.dumps({"request": "...", "tokensIn": 1200, "tokensOut": 340,
                             "cacheWrites": 50, "cacheReads": 10, "cost": 0.0042})},
        # read_file tool: result comes ONLY from api_conversation_history.
        {"ts": 1150, "type": "say", "say": "tool",
         "text": json.dumps({"tool": "readFile", "path": "src/parser.py",
                             "readLineStart": 1, "readLineEnd": 40})},
        {"ts": 1200, "type": "say", "say": "tool",
         "text": json.dumps({"tool": "editedExistingFile", "path": "src/parser.py",
                             "diff": EDIT_DIFF}),
         "lastCheckpointHash": "deadbeefcafe", "isCheckpointCheckedOut": False,
         "conversationHistoryIndex": 7,
         "conversationHistoryDeletedRange": [2, 4]},
        {"ts": 1250, "type": "say", "say": "browser_action",
         "text": json.dumps({"action": "launch", "url": "https://example.com"})},
        {"ts": 1260, "type": "say", "say": "browser_action_result",
         "text": json.dumps({"screenshot": SCREENSHOT_B64,
                             "logs": "console clean",
                             "currentUrl": "https://example.com/"})},
        {"ts": 1300, "type": "say", "say": "command", "text": "pytest -q"},
        {"ts": 1400, "type": "say", "say": "command_output", "text": "3 passed"},
        {"ts": 1500, "type": "say", "say": "completion_result",
         "text": "Done. The parser was refactored and tests pass."},
    ]
    (task_dir / "ui_messages.json").write_text(json.dumps(ui_messages), encoding="utf-8")

    # tool_use blocks in document order: readFile, edit, browser, command. Each
    # paired (by tool_use_id) with the following tool_result; one thinking block.
    api_history = [
        {"role": "user", "content": "Refactor the parser"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "I should read the file first."},
            {"type": "text", "text": "Reading the file."},
            {"type": "tool_use", "id": "tu_read", "name": "read_file",
             "input": {"path": "src/parser.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_read",
             "content": "1 | import os\n2 | old_line_one\n3 | old_line_two\n"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_edit", "name": "replace_in_file",
             "input": {"path": "src/parser.py"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_edit",
             "content": [{"type": "text", "text": "The content was successfully saved."}]},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_browser", "name": "browser_action",
             "input": {"action": "launch", "url": "https://example.com"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_browser",
             "content": "screenshot captured"},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_cmd", "name": "execute_command",
             "input": {"command": "pytest -q"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_cmd", "content": "3 passed"},
        ]},
    ]
    (task_dir / "api_conversation_history.json").write_text(
        json.dumps(api_history), encoding="utf-8")

    metadata = {
        "files_in_context": [
            {"path": "src/parser.py", "record_state": "active",
             "record_source": "cline_edited", "cline_read_date": 1150,
             "cline_edit_date": 1200},
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


def _capsules(db_path: Path, tenant_id=None) -> list[dict]:
    storage = CapsuleStorage(db_path)
    try:
        rows = storage.get_all_ordered(tenant_id=tenant_id)
        return [json.loads(r["canonical"]) for r in rows]
    finally:
        storage.close()


def _capsule_types(db_path: Path, tenant_id=None) -> list[str]:
    return [c.get("type") for c in _capsules(db_path, tenant_id)]


def _tool_calls(capsule: dict) -> list[dict]:
    return (capsule.get("execution") or {}).get("tool_calls") or []


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

    capsules = _capsules(db_path)
    types = [c.get("type") for c in capsules]
    assert "tool" in types, f"expected a tool capsule, got {types}"
    assert "chat" in types, f"expected a chat capsule, got {types}"
    # Session-provenance system capsule leads the chain.
    assert "system" in types, f"expected a system capsule, got {types}"
    prov = next(c for c in capsules
                if c.get("type") == "system"
                and (c.get("outcome") or {}).get("summary") == "session provenance")
    prov_result = (prov.get("outcome") or {}).get("result") or {}
    assert prov_result.get("files_in_context"), "provenance should list files_in_context"
    assert prov_result.get("model_usage"), "provenance should list model_usage"

    # (b) A non-command tool capsule's result is populated from the api history.
    read_calls = [tc for c in capsules for tc in _tool_calls(c)
                  if tc.get("tool") == "readFile"]
    assert read_calls, "expected a readFile tool capsule"
    read_result = read_calls[0].get("result")
    assert read_result is not None, "readFile result must come from api history, not None"
    assert "import os" in str(read_result), f"unexpected read result: {read_result!r}"
    # Line-range arguments captured.
    assert read_calls[0]["arguments"].get("readLineStart") == 1

    # (c) The edit capsule carries +/- counts in its summary and an edited side
    # effect; the diff added 3 lines and removed 2.
    edit_caps = [c for c in capsules
                 if any(tc.get("tool") == "editedExistingFile" for tc in _tool_calls(c))]
    assert edit_caps, "expected an editedExistingFile capsule"
    edit_cap = edit_caps[0]
    edit_summary = (edit_cap.get("outcome") or {}).get("summary", "")
    assert "(+3/-2)" in edit_summary, f"expected +/- counts, got {edit_summary!r}"
    side = (edit_cap.get("outcome") or {}).get("side_effects") or []
    assert any("edited" in s for s in side), f"expected an edited side effect, got {side}"
    # Checkpoint + truncation provenance landed in env.
    edit_env = (edit_cap.get("context") or {}).get("environment") or {}
    assert edit_env.get("last_checkpoint_hash") == "deadbeefcafe"
    assert edit_env.get("conversation_history_index") == 7

    # (d) The browser capsule records a screenshot reference, never the base64.
    browser_caps = [c for c in capsules
                    if any(tc.get("tool") == "browser_action" for tc in _tool_calls(c))]
    assert browser_caps, "expected a browser_action capsule"
    browser_result = next(tc for tc in _tool_calls(browser_caps[0])
                          if tc.get("tool") == "browser_action").get("result")
    assert isinstance(browser_result, dict), f"browser result: {browser_result!r}"
    assert browser_result.get("has_screenshot") is True
    assert browser_result.get("screenshot_bytes", 0) > 0
    # The base64 payload must be absent from the entire serialized capsule.
    assert "AAAAAAAA" not in json.dumps(browser_caps[0]), "base64 leaked into capsule"


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
