# SPDX-License-Identifier: Apache-2.0
"""Tests for the Claude Code adapter: parse a synthetic transcript, seal it,
verify the chain, and confirm the rich `toolUseResult` enrichment (real diffs,
Bash stdout, subagent scorecards) lands in the capsules. One test also confirms
a one-byte tamper breaks verification."""

from __future__ import annotations

import json

from agent_capsule.adapters import claude_code
from agent_capsule.core import paths
from agent_capsule.core.chain import CapsuleChain
from agent_capsule.core.seal import Seal
from agent_capsule.core.storage import CapsuleStorage

SESSION = "cc-test-1"


def _transcript(path) -> str:
    """Write a synthetic Claude Code transcript JSONL covering Edit (with a
    structuredPatch), Bash (stdout), and Agent (subagent scorecard)."""
    records = [
        {"type": "user", "uuid": "u0", "timestamp": "2026-05-31T00:00:00Z",
         "message": {"role": "user", "content": "fix the bug and run the tests"}},
        # assistant: text + Edit tool call
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-05-31T00:00:01Z",
         "permissionMode": "acceptEdits", "cwd": "/repo", "gitBranch": "main", "version": "2.0",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "stop_reason": "tool_use",
                     "usage": {"input_tokens": 100, "output_tokens": 20,
                               "cache_read_input_tokens": 50},
                     "content": [{"type": "text", "text": "I'll patch app.py."},
                                 {"type": "tool_use", "id": "t1", "name": "Edit",
                                  "input": {"file_path": "app.py", "old_string": "x", "new_string": "y"}}]}},
        {"type": "user", "uuid": "u1", "timestamp": "2026-05-31T00:00:02Z",
         "toolUseResult": {"filePath": "app.py", "userModified": False,
                           "originalFile": "x" * 5000,  # heavy: must be dropped from the capsule
                           "structuredPatch": [{"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 2,
                                                 "lines": [" ctx", "-x", "+y", "+z"]}]},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        # assistant: Bash tool call
        {"type": "assistant", "uuid": "a2", "timestamp": "2026-05-31T00:00:03Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "stop_reason": "tool_use",
                     "usage": {"input_tokens": 110, "output_tokens": 8},
                     "content": [{"type": "tool_use", "id": "t2", "name": "Bash",
                                  "input": {"command": "pytest -q"}}]}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-05-31T00:00:04Z",
         "toolUseResult": {"stdout": "3 passed in 0.10s", "stderr": "", "interrupted": False},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "3 passed in 0.10s"}]}},
        # assistant: Agent (subagent) tool call
        {"type": "assistant", "uuid": "a3", "timestamp": "2026-05-31T00:00:05Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "stop_reason": "tool_use",
                     "usage": {"input_tokens": 120, "output_tokens": 9},
                     "content": [{"type": "tool_use", "id": "t3", "name": "Agent",
                                  "input": {"description": "audit deps", "subagent_type": "Explore"}}]}},
        {"type": "user", "uuid": "u3", "timestamp": "2026-05-31T00:00:08Z",
         "toolUseResult": {"agentType": "Explore", "totalToolUseCount": 9,
                           "toolStats": {"editFileCount": 2, "linesAdded": 14, "linesRemoved": 3,
                                         "readCount": 5, "bashCount": 1},
                           "content": [{"type": "text", "text": "m1"}, {"type": "text", "text": "m2"}]},
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": "audit complete"}]}},
        # final assistant text
        {"type": "assistant", "uuid": "a4", "timestamp": "2026-05-31T00:00:09Z",
         "message": {"role": "assistant", "model": "claude-opus-4-8", "stop_reason": "end_turn",
                     "usage": {"input_tokens": 130, "output_tokens": 12},
                     "content": [{"type": "text", "text": "Done. Bug fixed, tests pass."}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return str(path)


def _point_home(monkeypatch, tmp_path):
    home = tmp_path / "ac-home"
    monkeypatch.setenv("AGENT_CAPSULE_HOME", str(home))
    monkeypatch.delenv("AGENT_CAPSULE_DB", raising=False)
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "CHAINS_DIR", home / "chains")
    monkeypatch.setattr(paths, "KEY_PATH", home / "key")
    monkeypatch.setattr(paths, "LOG_PATH", home / "hook.log")
    return home


def _capsules(home):
    db = home / "chains" / "claude-code" / f"{SESSION}.db"
    storage = CapsuleStorage(db)
    rows = [json.loads(r["canonical"]) for r in storage.get_all_ordered()]
    storage.close()
    return db, rows


def _tool_call(d):
    tcs = d.get("execution", {}).get("tool_calls", [])
    return tcs[0] if tcs else {}


def test_claude_code_enrichment(tmp_path, monkeypatch):
    home = _point_home(monkeypatch, tmp_path)
    transcript = _transcript(tmp_path / "session.jsonl")

    claude_code.run(SESSION, transcript, finalize=True)

    db, rows = _capsules(home)
    assert CapsuleChain(CapsuleStorage(db)).verify(seal=Seal()).valid is True

    by_tool = {_tool_call(d).get("tool"): d for d in rows if _tool_call(d).get("tool")}

    # Edit -> rendered diff + (+/-) counts in summary, side effect, no heavy originalFile.
    edit = by_tool["Edit"]
    assert "(+2/-1)" in edit["outcome"]["summary"]
    assert "@@" in str(_tool_call(edit)["result"])
    assert any("edited app.py" in s for s in edit["outcome"]["side_effects"])
    assert "originalFile" not in json.dumps(edit)  # heavy field trimmed

    # Bash -> stdout in result.
    bash = by_tool["Bash"]
    assert "3 passed" in str(_tool_call(bash)["result"])

    # Agent -> subagent scorecard in summary + delegated side effect.
    agent = by_tool["Agent"]
    assert "Explore" in agent["outcome"]["summary"]
    assert "9 tool calls" in agent["outcome"]["summary"]
    assert "+14/-3" in agent["outcome"]["summary"]
    assert any("delegated to Explore subagent" in s for s in agent["outcome"]["side_effects"])

    # Cache token usage carried through.
    assert edit["execution"]["resources_used"].get("cache_read_input_tokens") == 50


def test_claude_code_tamper_breaks(tmp_path, monkeypatch):
    home = _point_home(monkeypatch, tmp_path)
    transcript = _transcript(tmp_path / "session.jsonl")
    claude_code.run(SESSION, transcript, finalize=True)

    db = home / "chains" / "claude-code" / f"{SESSION}.db"
    conn = __import__("sqlite3").connect(str(db))
    row = conn.execute("SELECT canonical FROM capsules WHERE sequence=0").fetchone()[0]
    conn.execute("UPDATE capsules SET canonical=? WHERE sequence=0",
                 (row.replace("fix the bug", "f1x the bug", 1),))
    conn.commit()
    conn.close()

    result = CapsuleChain(CapsuleStorage(db)).verify(seal=Seal())
    assert result.valid is False
    assert result.broken_at == 0
