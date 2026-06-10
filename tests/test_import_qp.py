# SPDX-License-Identifier: Apache-2.0
"""Round-trip test for the legacy qp_capsule importer (scripts/import_qp_chains.py).

The migration's core invariant: every capsule's stored hash and signature survive
unchanged, because the only schema difference is a ``data -> canonical`` rename
(re-canonicalized to the exact hashed bytes) and ``session_id -> tenant_id``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

from nacl.signing import SigningKey

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_qp_chains.py"
_spec = importlib.util.spec_from_file_location("import_qp_chains", _SCRIPT)
qp_import = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qp_import)


def _make_qp_db(path: Path, session_id: str, n: int = 3) -> str:
    """Write a synthetic legacy qp_capsule chain DB. Returns the signer pubkey hex."""
    sk = SigningKey.generate()
    pub = sk.verify_key.encode().hex()
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE capsules (id TEXT, type TEXT, sequence INTEGER, previous_hash TEXT, "
        "data TEXT, hash TEXT, signature TEXT, signature_pq TEXT, signed_at TEXT, "
        "signed_by TEXT, session_id TEXT)"
    )
    prev = None
    for i in range(n):
        d = {
            "id": f"c{i}", "type": "chat", "domain": "claude-code", "parent_id": None,
            "sequence": i, "previous_hash": prev,
            "trigger": {"request": f"req {i}"}, "context": {"session_id": session_id},
            "reasoning": {}, "authority": {}, "execution": {}, "outcome": {"summary": f"out {i}"},
        }
        canon = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        h = hashlib.sha3_256(canon.encode("utf-8")).hexdigest()
        sig = sk.sign(h.encode("utf-8")).signature.hex()
        conn.execute(
            "INSERT INTO capsules VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"c{i}", "chat", i, prev, canon, h, sig, "", f"2026-01-0{i + 1}T00:00:00", pub[:16], session_id),
        )
        prev = h
    conn.commit()
    conn.close()
    return pub


def test_import_preserves_hashes_and_links(tmp_path):
    src = tmp_path / "qp" / "sessA.db"
    src.parent.mkdir()
    _make_qp_db(src, "sessA", 3)
    dest = tmp_path / "out" / "sessA.db"

    status, n, fails = qp_import.import_chain(src, dest, dry_run=False)
    assert (status, n, fails) == ("imported", 3, 0)

    conn = sqlite3.connect(str(dest))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("SELECT * FROM capsules ORDER BY sequence"))
    conn.close()

    assert len(rows) == 3
    prev = None
    for r in rows:
        # Hash reproduces from the migrated canonical bytes...
        assert hashlib.sha3_256(r["canonical"].encode("utf-8")).hexdigest() == r["hash"]
        # ...and the chain links are intact.
        assert r["previous_hash"] == prev
        assert r["tenant_id"] is None  # per-file layout
        prev = r["hash"]


def test_import_is_idempotent(tmp_path):
    src = tmp_path / "qp" / "s.db"
    src.parent.mkdir()
    _make_qp_db(src, "s", 2)
    dest = tmp_path / "out" / "s.db"

    qp_import.import_chain(src, dest, dry_run=False)
    status, n, _ = qp_import.import_chain(src, dest, dry_run=False)
    assert status.startswith("skip")
    assert n == 2


def test_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "qp" / "s.db"
    src.parent.mkdir()
    _make_qp_db(src, "s", 2)
    dest = tmp_path / "out" / "s.db"

    status, n, fails = qp_import.import_chain(src, dest, dry_run=True)
    assert status == "would import"
    assert n == 2 and fails == 0
    assert not dest.exists()


def test_tampered_source_is_detected(tmp_path):
    src = tmp_path / "qp" / "s.db"
    src.parent.mkdir()
    _make_qp_db(src, "s", 3)
    # Corrupt one capsule's *content* so it no longer hashes to its stored hash.
    # (A whitespace-only change wouldn't count: re-canonicalization is robust to it.)
    conn = sqlite3.connect(str(src))
    conn.execute("UPDATE capsules SET data = REPLACE(data, 'out 1', 'TAMPERED') WHERE sequence = 1")
    conn.commit()
    conn.close()

    status, n, fails = qp_import.import_chain(src, tmp_path / "out" / "s.db", dry_run=False)
    assert status.startswith("ABORT")
    assert fails >= 1
    assert not (tmp_path / "out" / "s.db").exists()
