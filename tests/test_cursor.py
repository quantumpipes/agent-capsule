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
                "toolFormerData": {
                    "name": "run_terminal_cmd",
                    "rawArgs": json.dumps({"command": "pytest -q", "cwd": "/repo"}),
                    "params": {"command": "pytest -q", "cwd": "/repo"},
                    "result": json.dumps({"exitCode": 0, "output": "5 passed"}),
                    "status": "completed",
                },
            },
        }
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


def test_seals_tool_and_chat_from_db(env_home, tmp_path):
    import agent_capsule.adapters.cursor as cursor
    from agent_capsule.core.chain import CapsuleChain
    from agent_capsule.core.seal import Seal
    from agent_capsule.core.storage import CapsuleStorage

    vscdb = tmp_path / "state.vscdb"
    transcript = tmp_path / "transcript.jsonl"
    _write_vscdb(vscdb)
    _write_transcript(transcript)

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

    types = {json.loads(r["canonical"])["type"] for r in rows}
    assert "tool" in types, "expected a tool capsule"
    assert "chat" in types, "expected a chat capsule"

    # Tool capsule should carry the tool name and prompt from the preceding user turn.
    canonicals = [json.loads(r["canonical"]) for r in rows]
    tool_caps = [c for c in canonicals if c["type"] == "tool"]
    assert tool_caps
    tc = tool_caps[0]
    assert tc["execution"]["tool_calls"][0]["tool"] == "run_terminal_cmd"
    assert tc["trigger"]["request"] == "Please run the tests"


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
