# SPDX-License-Identifier: Apache-2.0
"""Tests for the Cursor adapter.

Build a synthetic Cursor globalStorage ``state.vscdb`` (table ``cursorDiskKV``)
plus a transcript JSONL, drive the adapter in offline ``--db/--transcript/
--session/--finalize`` mode, then verify the resulting sealed chain.

Run with:
    PYTHONPATH=src AGENT_CAPSULE_HOME=/tmp/x python3 -m pytest tests/test_cursor.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CONV_ID = "conv-abc123"


def _write_vscdb(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)"
        )
        composer = {
            "name": "Fix the parser",
            "createdAt": 1_717_000_000_000,
            "contextTokensUsed": 4096,
            "status": "completed",
            "modelConfig": {"modelName": "claude-3.7-sonnet"},
            "fullConversationHeadersOnly": [
                {"bubbleId": "b1", "type": 1, "serverBubbleId": "s1"},
                {"bubbleId": "b2", "type": 2, "serverBubbleId": "s2"},
                {"bubbleId": "b3", "type": 2, "serverBubbleId": "s3"},
            ],
        }
        bubbles = {
            "b1": {
                "type": 1,
                "createdAt": "2026-05-30T10:00:00Z",
                "text": "<user_query>Please run the tests</user_query>",
                "tokenCount": {"inputTokens": 12, "outputTokens": 0},
            },
            "b2": {
                "type": 2,
                "createdAt": "2026-05-30T10:00:05Z",
                "text": "I'll run the test suite now.",
                "tokenCount": {"inputTokens": 12, "outputTokens": 34},
            },
            "b3": {
                "type": 2,
                "createdAt": "2026-05-30T10:00:06Z",
                "text": "",
                "tokenCount": {"inputTokens": 0, "outputTokens": 8},
                "thinking": "I should run pytest to confirm the suite is green.",
                "thinkingDurationMs": 1200,
                "turnDurationMs": 3400,
                "modelInfo": {"modelName": "claude-3.7-sonnet"},
                "contextWindowStatusAtCreation": {
                    "tokensUsed": 4096, "tokenLimit": 200000,
                    "percentageRemaining": 97.9,
                },
                "toolFormerData": {
                    "name": "run_terminal_cmd",
                    "rawArgs": json.dumps({"command": "pytest -q", "cwd": "/repo"}),
                    "params": {"command": "pytest -q", "cwd": "/repo"},
                    "result": json.dumps({"exitCode": 0, "output": "5 passed"}),
                    "status": "completed",
                },
            },
            "b4": {
                "type": 2,
                "createdAt": "2026-05-30T10:00:07Z",
                "text": "",
                "tokenCount": {"inputTokens": 0, "outputTokens": 20},
                "toolFormerData": {
                    "name": "edit_file_v2",
                    "params": {
                        "target_file": "src/parser.py",
                        "old_string": "def parse(x):\n    return x",
                        "new_string": "def parse(x):\n    return x.strip()\n    # done",
                    },
                    "result": json.dumps({"applied": True}),
                    "status": "completed",
                },
            },
        }
        composer["fullConversationHeadersOnly"].append(
            {"bubbleId": "b4", "type": 2, "serverBubbleId": "s4"}
        )
        conn.execute(
            "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
            (f"composerData:{CONV_ID}", json.dumps(composer)),
        )
        for bid, body in bubbles.items():
            conn.execute(
                "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (f"bubbleId:{CONV_ID}:{bid}", json.dumps(body)),
            )
        conn.commit()
    finally:
        conn.close()


def _write_ai_tracking_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE ai_code_hashes (hash TEXT, source TEXT, fileName TEXT, "
            "requestId TEXT, conversationId TEXT, model TEXT, timestamp INTEGER)"
        )
        conn.execute(
            "CREATE TABLE scored_commits (commitHash TEXT, branchName TEXT, "
            "linesAdded INTEGER, composerLinesAdded INTEGER, humanLinesAdded INTEGER, "
            "commitMessage TEXT, commitDate INTEGER, v1AiPercentage REAL, "
            "v2AiPercentage REAL)"
        )
        hashes = [
            ("h1", "composer", "src/parser.py", "req-1", CONV_ID, "claude-3.7-sonnet", 1),
            ("h2", "composer", "src/util.py", "req-1", CONV_ID, "claude-3.7-sonnet", 2),
            ("h3", "human", "src/parser.py", "req-2", CONV_ID, "claude-3.7-sonnet", 3),
            # A row for a different conversation that must be ignored.
            ("h4", "composer", "other.py", "req-9", "conv-other", "gpt-5", 4),
        ]
        conn.executemany(
            "INSERT INTO ai_code_hashes (hash, source, fileName, requestId, "
            "conversationId, model, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
            hashes,
        )
        conn.execute(
            "INSERT INTO scored_commits (commitHash, branchName, linesAdded, "
            "composerLinesAdded, humanLinesAdded, commitMessage, commitDate, "
            "v1AiPercentage, v2AiPercentage) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("abc123def456", "main", 30, 24, 6, "fix parser", 1_717_000_500_000, 0.7, 0.8),
        )
        conn.commit()
    finally:
        conn.close()


def _write_transcript(path: Path) -> None:
    lines = [
        {"role": "user", "message": {"content": [
            {"type": "text", "text": "<user_query>Please run the tests</user_query>"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "I'll run the test suite now."}]}},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")


@pytest.fixture()
def env_home(tmp_path, monkeypatch):
    home = tmp_path / "agent-capsule-home"
    monkeypatch.setenv("AGENT_CAPSULE_HOME", str(home))
    monkeypatch.delenv("AGENT_CAPSULE_DB", raising=False)
    # Never read a real on-disk AI tracking DB during tests; opt in per-test.
    monkeypatch.setenv("AI_TRACKING_DB", str(tmp_path / "no-such-ai-tracking.db"))
    # paths.HOME is computed at import time, so reload modules that captured it.
    import importlib
    import agent_capsule.core.paths as paths
    import agent_capsule.core.sealing as sealing
    import agent_capsule.adapters.cursor as cursor
    importlib.reload(paths)
    importlib.reload(sealing)
    importlib.reload(cursor)
    return home


def _chain_db(home: Path) -> Path:
    return home / "chains" / "cursor" / f"{CONV_ID}.db"


def test_seals_tool_and_chat_from_db(env_home, tmp_path, monkeypatch):
    import agent_capsule.adapters.cursor as cursor
    from agent_capsule.core.chain import CapsuleChain
    from agent_capsule.core.seal import Seal
    from agent_capsule.core.storage import CapsuleStorage

    vscdb = tmp_path / "state.vscdb"
    transcript = tmp_path / "transcript.jsonl"
    ai_db = tmp_path / "ai-code-tracking.db"
    _write_vscdb(vscdb)
    _write_transcript(transcript)
    _write_ai_tracking_db(ai_db)
    monkeypatch.setenv("AI_TRACKING_DB", str(ai_db))

    rc = cursor.main([
        "--db", str(vscdb),
        "--transcript", str(transcript),
        "--session", CONV_ID,
        "--finalize",
    ])
    assert rc == 0

    db = _chain_db(env_home)
    assert db.exists(), "chain DB was not created"

    storage = CapsuleStorage(db)
    try:
        chain = CapsuleChain(storage)
        result = chain.verify(seal=Seal())
        assert result.valid is True
        rows = storage.get_all_ordered()
    finally:
        storage.close()

    canonicals = [json.loads(r["canonical"]) for r in rows]
    types = {c["type"] for c in canonicals}
    assert "tool" in types, "expected a tool capsule"
    assert "chat" in types, "expected a chat capsule"
    assert "system" in types, "expected an authorship system capsule"

    # The authorship system capsule leads the chain and summarizes AI vs human work.
    sys_caps = [c for c in canonicals if c["type"] == "system"]
    assert sys_caps
    auth = sys_caps[0]
    assert canonicals[0]["type"] == "system", "authorship capsule should lead the chain"
    assert "AI authorship" in auth["outcome"]["summary"]
    structured = auth["outcome"]["result"]
    assert structured["ai_authored_edits"] == 2, "two composer-sourced edits for this conv"
    assert structured["total_tracked_edits"] == 3, "other conversation rows excluded"
    assert "src/parser.py" in structured["files_touched"]
    assert structured["commit_count"] >= 1
    assert structured["commits"][0]["v2_ai_percentage"] == 0.8

    # Tool capsule should carry the tool name and prompt from the preceding user turn.
    tool_caps = [c for c in canonicals if c["type"] == "tool"]
    assert tool_caps
    run_caps = [c for c in tool_caps
                if c["execution"]["tool_calls"][0]["tool"] == "run_terminal_cmd"]
    assert run_caps
    tc = run_caps[0]
    assert tc["trigger"]["request"] == "Please run the tests"
    # Telemetry + plaintext reasoning landed on the run_terminal_cmd turn.
    assert tc["context"]["environment"].get("turn_duration_ms") == 3400
    assert tc["context"]["environment"].get("context_window", {}).get("token_limit") == 200000
    assert "pytest" in tc["reasoning"]["reasoning"]

    # The edit_file_v2 turn renders a diff and records the edited path.
    edit_caps = [c for c in tool_caps
                 if c["execution"]["tool_calls"][0]["tool"] == "edit_file_v2"]
    assert edit_caps
    ec = edit_caps[0]
    assert any("edited src/parser.py" == s for s in ec["outcome"]["side_effects"]), \
        "edit must record the edited path as a side effect"
    assert "+" in ec["outcome"]["summary"] and "-" in ec["outcome"]["summary"], \
        "edit summary should carry +/- line counts"
    diff = ec["execution"]["tool_calls"][0]["result"]
    assert isinstance(diff, str) and "+    return x.strip()" in diff


def test_tamper_breaks_verification(env_home, tmp_path):
    import agent_capsule.adapters.cursor as cursor
    from agent_capsule.core.chain import CapsuleChain
    from agent_capsule.core.seal import Seal
    from agent_capsule.core.storage import CapsuleStorage

    vscdb = tmp_path / "state.vscdb"
    _write_vscdb(vscdb)
    cursor.main(["--db", str(vscdb), "--session", CONV_ID, "--finalize"])

    db = _chain_db(env_home)
    assert db.exists()

    # Flip one byte of a canonical payload (raw sqlite write, bypassing the chain).
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT rowid_pk, canonical FROM capsules ORDER BY sequence ASC LIMIT 1"
        ).fetchone()
        rowid, canonical = row[0], row[1]
        tampered = ("X" if canonical[0] != "X" else "Y") + canonical[1:]
        conn.execute(
            "UPDATE capsules SET canonical = ? WHERE rowid_pk = ?", (tampered, rowid)
        )
        conn.commit()
    finally:
        conn.close()

    storage = CapsuleStorage(db)
    try:
        result = CapsuleChain(storage).verify(seal=Seal())
    finally:
        storage.close()
    assert result.valid is False


def test_transcript_fallback_when_no_db(env_home, tmp_path):
    import agent_capsule.adapters.cursor as cursor
    from agent_capsule.core.chain import CapsuleChain
    from agent_capsule.core.seal import Seal
    from agent_capsule.core.storage import CapsuleStorage

    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript)

    # No --db: must fall back to the transcript and still seal a chat capsule.
    rc = cursor.main(["--transcript", str(transcript), "--session", CONV_ID, "--finalize"])
    assert rc == 0

    db = _chain_db(env_home)
    assert db.exists()
    storage = CapsuleStorage(db)
    try:
        assert CapsuleChain(storage).verify(seal=Seal()).valid is True
        rows = storage.get_all_ordered()
    finally:
        storage.close()
    types = [json.loads(r["canonical"])["type"] for r in rows]
    assert "chat" in types


def test_install_uninstall_idempotent(tmp_path, monkeypatch):
    import agent_capsule.adapters.cursor as cursor

    hooks_path = tmp_path / "hooks.json"
    # Pre-existing unrelated hook must be preserved.
    hooks_path.write_text(json.dumps({
        "version": 1,
        "hooks": {"stop": [{"command": "someone-elses-hook", "type": "command"}]},
    }))
    monkeypatch.setattr(cursor, "HOOKS_JSON", hooks_path)

    cursor.install()
    cursor.install()  # idempotent
    data = json.loads(hooks_path.read_text())
    assert data["version"] == 1
    stop = data["hooks"]["stop"]
    cmds = [h["command"] for h in stop]
    assert cmds.count(cursor.HOOK_COMMAND) == 1
    assert "someone-elses-hook" in cmds
    assert any(h["command"] == cursor.HOOK_COMMAND for h in data["hooks"]["sessionEnd"])

    cursor.uninstall()
    data = json.loads(hooks_path.read_text())
    stop = data["hooks"].get("stop", [])
    assert all(h["command"] != cursor.HOOK_COMMAND for h in stop)
    assert "someone-elses-hook" in [h["command"] for h in stop]
    assert "sessionEnd" not in data["hooks"]
