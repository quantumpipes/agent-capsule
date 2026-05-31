# SPDX-License-Identifier: Apache-2.0
"""Tests for the Codex CLI adapter.

Runnable via:
    PYTHONPATH=src AGENT_CAPSULE_HOME=/tmp/x python3 -m pytest tests/test_codex.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_capsule.adapters import codex
from agent_capsule.core.chain import CapsuleChain
from agent_capsule.core.seal import Seal
from agent_capsule.core.storage import CapsuleStorage

SESSION = "test-thread"


APPLY_PATCH = (
    "*** Begin Patch\n"
    "*** Update File: src/app.py\n"
    "@@ def main():\n"
    "-    print('old')\n"
    "+    print('new')\n"
    "+    return 0\n"
    "*** End Patch"
)


def _write_rollout(path: Path) -> None:
    lines = [
        {"timestamp": "2026-05-30T12:00:00Z", "type": "session_meta",
         "payload": {"id": SESSION, "cwd": "/tmp/work", "cli_version": "0.9.0",
                     "originator": "cli", "model_provider": "openai",
                     "base_instructions": "You are Codex, a coding agent.",
                     "git_branch": "main", "git_sha": "abc123"}},
        {"timestamp": "2026-05-30T12:00:01Z", "type": "turn_context",
         "payload": {"model": "gpt-5-codex", "approval_policy": "on-request",
                     "sandbox_policy": "workspace-write", "cwd": "/tmp/work",
                     "effort": "high", "summary": "auto", "timezone": "America/Chicago"}},
        {"timestamp": "2026-05-30T12:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started"}},
        {"timestamp": "2026-05-30T12:00:03Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "fix the bug then list files"}},
        {"timestamp": "2026-05-30T12:00:035Z", "type": "response_item",
         "payload": {"type": "reasoning",
                     "summary": [{"type": "summary_text", "text": "Plan the edit."}],
                     "content": [{"type": "reasoning_text",
                                  "text": "I will update app.py to print new."}],
                     "encrypted_content": "REDACTED=="}},
        {"timestamp": "2026-05-30T12:00:036Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "apply_patch",
                     "arguments": json.dumps({"input": APPLY_PATCH}),
                     "call_id": "call-0"}},
        {"timestamp": "2026-05-30T12:00:037Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call-0",
                     "output": {"output": "Success. Updated src/app.py",
                                "metadata": {"exit_code": 0}}}},
        {"timestamp": "2026-05-30T12:00:04Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": json.dumps({"command": ["ls", "-la"]}),
                     "call_id": "call-1"}},
        {"timestamp": "2026-05-30T12:00:05Z", "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "call-1",
                     "output": "total 0\ndrwxr-xr-x  2 user  staff  64 ."}},
        {"timestamp": "2026-05-30T12:00:06Z", "type": "event_msg",
         "payload": {"type": "agent_message",
                     "message": "The directory is empty except for itself."}},
        {"timestamp": "2026-05-30T12:00:065Z", "type": "compacted",
         "payload": {"message": "Earlier: edited app.py and listed files."}},
        {"timestamp": "2026-05-30T12:00:07Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {"input_tokens": 120,
                              "output_tokens": 30, "cached_input_tokens": 8,
                              "reasoning_output_tokens": 10, "total_tokens": 150},
                              "model_context_window": 272000},
                     "rate_limits": {"plan_type": "pro",
                                     "primary": {"used_percent": 12.5}}}},
        {"timestamp": "2026-05-30T12:00:08Z", "type": "event_msg",
         "payload": {"type": "task_complete"}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "agent-capsule-home"
    monkeypatch.setenv("AGENT_CAPSULE_HOME", str(h))
    monkeypatch.delenv("AGENT_CAPSULE_DB", raising=False)
    # paths reads HOME at import; reload the modules that captured it
    import importlib

    from agent_capsule.core import paths as paths_mod
    importlib.reload(paths_mod)
    from agent_capsule.core import sealing as sealing_mod
    importlib.reload(sealing_mod)
    importlib.reload(codex)
    return h


def _db_path(home: Path) -> Path:
    return home / "chains" / "codex" / f"{SESSION}.db"


def test_seals_and_verifies(home, tmp_path):
    rollout = tmp_path / "rollout-2026-05-30T12-00-00-uuid.jsonl"
    _write_rollout(rollout)

    rc = codex.main(["--rollout", str(rollout), "--session", SESSION, "--finalize"])
    assert rc == 0

    db = _db_path(home)
    assert db.exists(), f"chain DB not created at {db}"

    storage = CapsuleStorage(db)
    try:
        result = CapsuleChain(storage).verify(seal=Seal())
        assert result.valid is True
        rows = storage.get_all_ordered()
        canon = [json.loads(r["canonical"]) for r in rows]

        # a tool capsule for the shell function_call, carrying its result + usage
        tools = [c for c in canon if c["type"] == "tool"]
        assert tools, "expected a tool capsule"
        shell = next(c for c in tools
                     if c["execution"]["tool_calls"][0]["tool"] == "shell")
        assert shell["execution"]["tool_calls"][0]["arguments"] == {"command": ["ls", "-la"]}
        assert "total 0" in str(shell["execution"]["tool_calls"][0]["result"])
        # token_count attaches usage to the most recent spec (the tool was last
        # before the agent_message; usage lands on the agent_message chat spec)

        # a chat capsule for the assistant agent_message
        chats = [c for c in canon if c["type"] == "chat"]
        assert chats, "expected a chat capsule"
        assert any("empty" in (c["outcome"].get("result") or "") for c in chats)

        # usage was captured somewhere in the chain, with the full breakdown
        usage_caps = [c for c in canon
                      if c["execution"]["resources_used"].get("total_tokens") == 150]
        assert usage_caps, "token usage not attached"
        ru = usage_caps[0]["execution"]["resources_used"]
        assert ru.get("cached_input_tokens") == 8 or ru.get("reasoning_output_tokens") == 10
        assert ru.get("model_context_window") == 272000

        # apply_patch capsule: rendered diff in result, +/- counts in summary,
        # "edited <path>" side effect.
        patch = next(c for c in tools
                     if c["execution"]["tool_calls"][0]["tool"] == "apply_patch")
        assert "(+2/-1)" in patch["outcome"]["summary"]
        diff = str(patch["execution"]["tool_calls"][0]["result"])
        assert "+    print('new')" in diff and "-    print('old')" in diff
        assert "edited src/app.py" in patch["outcome"]["side_effects"]
        # the raw V4A envelope is preserved in arguments
        assert "*** Begin Patch" in json.dumps(patch["execution"]["tool_calls"][0]["arguments"])

        # the function_call following the reasoning item carries the reasoning
        # text as hidden thinking (-> reasoning.reasoning).
        assert "update app.py" in patch["reasoning"]["reasoning"]

        # session base instructions captured as a system capsule
        systems = [c for c in canon if c["type"] == "system"]
        assert any("Codex, a coding agent" in (c["reasoning"].get("analysis") or "")
                   for c in systems), "base_instructions not captured"

        # history compaction captured as a system capsule
        assert any(c["outcome"]["summary"] == "history compaction" for c in systems)

        # turn_context extras land in the environment
        assert any(c["context"]["environment"].get("reasoning_effort") == "high"
                   for c in canon)
    finally:
        storage.close()


def test_idempotent_reseal(home, tmp_path):
    rollout = tmp_path / "rollout-x.jsonl"
    _write_rollout(rollout)

    codex.main(["--rollout", str(rollout), "--session", SESSION])
    db = _db_path(home)
    storage = CapsuleStorage(db)
    try:
        first = len(storage.get_all_ordered())
    finally:
        storage.close()

    # re-run on the same rollout: must append nothing new
    codex.main(["--rollout", str(rollout), "--session", SESSION, "--finalize"])
    storage = CapsuleStorage(db)
    try:
        second = len(storage.get_all_ordered())
    finally:
        storage.close()
    assert first == second, "per-turn reseal must be idempotent by key"


def test_tamper_breaks_verification(home, tmp_path):
    rollout = tmp_path / "rollout-t.jsonl"
    _write_rollout(rollout)
    codex.main(["--rollout", str(rollout), "--session", SESSION, "--finalize"])

    db = _db_path(home)
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT rowid_pk, canonical FROM capsules ORDER BY sequence ASC LIMIT 1"
        ).fetchone()
        rowid, canonical = row[0], row[1]
        # flip one byte of the canonical content
        tampered = ("X" + canonical[1:]) if canonical[0] != "X" else ("Y" + canonical[1:])
        conn.execute("UPDATE capsules SET canonical = ? WHERE rowid_pk = ?",
                     (tampered, rowid))
        conn.commit()
    finally:
        conn.close()

    storage = CapsuleStorage(db)
    try:
        result = CapsuleChain(storage).verify(seal=Seal())
        assert result.valid is False
        assert result.broken_at == 0
    finally:
        storage.close()


def test_finalize_inferred_from_task_complete(home, tmp_path):
    rollout = tmp_path / "rollout-f.jsonl"
    _write_rollout(rollout)
    # no --finalize flag; task_complete in the rollout should trigger verify
    session_id, specs, finalize = codex.build_plan(codex.load_lines(str(rollout)))
    assert session_id == SESSION
    assert finalize is True
    assert any(s["type"] == "tool" for s in specs)
