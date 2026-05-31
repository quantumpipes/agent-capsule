# SPDX-License-Identifier: Apache-2.0
"""SQLite storage for capsule chains.

One row per capsule. The canonical bytes and the seal fields are stored as
columns so the exporter can rebuild the verification bundle by reading columns
directly, with zero re-serialization (and therefore no chance of canonical
drift). ``(tenant_id, sequence)`` is unique so concurrent writers can't claim
the same slot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .capsule import Capsule

_SCHEMA = """
CREATE TABLE IF NOT EXISTS capsules (
    rowid_pk     INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT,
    sequence     INTEGER NOT NULL,
    id           TEXT NOT NULL,
    previous_hash TEXT,
    hash         TEXT NOT NULL,
    signature    TEXT NOT NULL,
    signature_pq TEXT NOT NULL DEFAULT '',
    signed_at    TEXT,
    signed_by    TEXT NOT NULL DEFAULT '',
    canonical    TEXT NOT NULL,
    UNIQUE (tenant_id, sequence)
);
"""


class CapsuleStorage:
    """A single SQLite file holding one or more capsule chains (by tenant_id)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get_latest(self, tenant_id: str | None = None) -> sqlite3.Row | None:
        cur = self._conn.execute(
            "SELECT * FROM capsules WHERE tenant_id IS ? ORDER BY sequence DESC LIMIT 1",
            (tenant_id,),
        )
        return cur.fetchone()

    def get_all_ordered(self, tenant_id: str | None = None) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM capsules WHERE tenant_id IS ? ORDER BY sequence ASC",
            (tenant_id,),
        )
        return list(cur.fetchall())

    def store(self, capsule: Capsule, tenant_id: str | None = None) -> Capsule:
        self._conn.execute(
            """INSERT INTO capsules
               (tenant_id, sequence, id, previous_hash, hash, signature,
                signature_pq, signed_at, signed_by, canonical)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tenant_id,
                capsule.sequence,
                capsule.id,
                capsule.previous_hash,
                capsule.hash,
                capsule.signature,
                capsule.signature_pq,
                capsule.signed_at,
                capsule.signed_by,
                capsule.canonical_bytes().decode("utf-8"),
            ),
        )
        self._conn.commit()
        return capsule

    def close(self) -> None:
        self._conn.close()
