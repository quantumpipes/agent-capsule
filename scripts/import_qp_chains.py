#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Import legacy qp_capsule (.quantumpipes/claude-chains) chains into the
agent-capsule store, byte-for-byte preserving every seal.

The two formats share the exact same wire format (SHA3-256 over
``json.dumps(to_dict(), sort_keys=True, separators=(",",":"), ensure_ascii=False)``
and ``Ed25519.sign(utf8(hash_hex))``), so a chain sealed by the old qp_capsule
library verifies unchanged once its rows are copied into the new schema.

The only schema differences are cosmetic:
  qp_capsule:    (id, type, sequence, previous_hash, data, hash, signature,
                  signature_pq, signed_at, signed_by, session_id)
  agent-capsule: (rowid_pk, tenant_id, sequence, id, previous_hash, hash,
                  signature, signature_pq, signed_at, signed_by, canonical)

We map ``data -> canonical`` (re-canonicalized to the exact hashed bytes) and
``session_id -> tenant_id``. We DO NOT round-trip through agent_capsule.Capsule:
its ``to_dict()`` injects a ``spec_version`` field the legacy capsules never had,
which would change the canonical bytes and break the hash. Storing the original
recanonicalized bytes directly keeps every hash and signature valid.

Idempotent: a target chain whose capsule count already matches is skipped.

Usage:
  python3 scripts/import_qp_chains.py                 # default src + dest
  python3 scripts/import_qp_chains.py --src DIR --dest DIR
  python3 scripts/import_qp_chains.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

SRC_DEFAULT = Path(os.path.expanduser("~/.quantumpipes/claude-chains"))
DEST_DEFAULT = Path(os.path.expanduser("~/.agent-capsule/chains/claude-code"))
QP_KEY_PATH = Path(os.path.expanduser("~/.quantumpipes/key"))


def register_qp_key() -> str | None:
    """Register the legacy qp_capsule public key so imported chains verify.

    Derives the Ed25519 public key from the local qp signing seed and adds it to
    the agent-capsule keyring (~/.agent-capsule/known_keys.json). Without this,
    imported chains pass hash verification but show an unknown signer.
    """
    if not QP_KEY_PATH.exists():
        return None
    try:
        from nacl.signing import SigningKey

        from agent_capsule.core import keyring

        seed = QP_KEY_PATH.read_bytes()[:32]
        pub = SigningKey(seed).verify_key.encode().hex()
        return keyring.register(pub, label="qp_capsule (legacy)")
    except Exception as e:  # noqa: BLE001 - registration is best-effort
        print(f"  (could not register qp key: {e})")
        return None

NEW_SCHEMA = """
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


def recanonicalize(data: str) -> str:
    """Re-serialize the legacy ``data`` blob into the exact hashed canonical bytes."""
    return json.dumps(
        json.loads(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def count_rows(db: Path) -> int:
    if not db.exists():
        return 0
    try:
        c = sqlite3.connect(str(db))
        n = c.execute("SELECT COUNT(*) FROM capsules").fetchone()[0]
        c.close()
        return int(n)
    except sqlite3.Error:
        return 0


def import_chain(src: Path, dest: Path, dry_run: bool) -> tuple[str, int, int]:
    """Return (status, capsules, hash_failures)."""
    sc = sqlite3.connect(str(src))
    sc.row_factory = sqlite3.Row
    rows = list(
        sc.execute(
            "SELECT id, sequence, previous_hash, data, hash, signature, "
            "signature_pq, signed_at, signed_by, session_id "
            "FROM capsules ORDER BY sequence ASC"
        )
    )
    sc.close()
    if not rows:
        return ("empty", 0, 0)

    if count_rows(dest) == len(rows):
        return ("skip (already imported)", len(rows), 0)

    # Verify every hash before we write a single byte.
    prepared = []
    hash_fail = 0
    for r in rows:
        canon = recanonicalize(r["data"])
        if hashlib.sha3_256(canon.encode("utf-8")).hexdigest() != r["hash"]:
            hash_fail += 1
        prepared.append((r, canon))
    if hash_fail:
        return (f"ABORT: {hash_fail} hash mismatch(es)", len(rows), hash_fail)

    if dry_run:
        return ("would import", len(rows), 0)

    if dest.exists():
        dest.unlink()  # we own this path; rebuild cleanly (idempotent re-run)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dc = sqlite3.connect(str(dest))
    dc.executescript(NEW_SCHEMA)
    dc.executemany(
        "INSERT INTO capsules (tenant_id, sequence, id, previous_hash, hash, "
        "signature, signature_pq, signed_at, signed_by, canonical) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                None,  # per-file layout = one chain per .db, NULL tenant (filename is the id)
                r["sequence"],
                r["id"],
                r["previous_hash"],
                r["hash"],
                r["signature"],
                r["signature_pq"] or "",
                r["signed_at"],
                r["signed_by"] or "",
                canon,
            )
            for (r, canon) in prepared
        ],
    )
    dc.commit()
    dc.close()
    return ("imported", len(rows), 0)


def seal_meta_chain(dest_dir: Path, reset: bool) -> None:
    """Seal every chain in ``dest_dir`` into the machine-wide meta-chain.

    The meta-chain records one entry per conversation (its head hash + capsule
    count), so its single head commits to every sealed conversation. Raw SQL
    import bypasses this, so we record the conversations explicitly here.

    With ``reset`` we first clear an existing meta-chain (e.g. stale test entries
    that point at deleted conversations) so the rebuilt chain is clean.
    """
    from agent_capsule.core import meta
    from agent_capsule.core.paths import META_CHECKPOINT, META_DB, META_LOCK

    if reset:
        for p in (META_DB, META_CHECKPOINT, META_LOCK):
            if p.exists():
                p.unlink()
        print("cleared existing meta-chain")

    recorded = 0
    for db in sorted(dest_dir.glob("*.db")):
        c = sqlite3.connect(str(db))
        row = c.execute(
            "SELECT hash, (SELECT COUNT(*) FROM capsules) FROM capsules "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        c.close()
        if not row:
            continue
        head_hash, count = row
        res = meta.record_conversation("claude-code", db.stem, head_hash, count)
        if res.get("recorded"):
            recorded += 1
    print(f"meta-chain: {recorded} conversation(s) sealed")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import legacy qp_capsule chains into agent-capsule")
    ap.add_argument("--src", default=str(SRC_DEFAULT), help="source dir of *.db (qp_capsule)")
    ap.add_argument("--dest", default=str(DEST_DEFAULT), help="dest dir (agent-capsule claude-code)")
    ap.add_argument("--dry-run", action="store_true", help="verify + report, write nothing")
    ap.add_argument("--seal-meta", action="store_true",
                    help="rebuild the meta-chain over all dest conversations (clears stale entries)")
    args = ap.parse_args(argv)

    src_dir = Path(os.path.expanduser(args.src))
    dest_dir = Path(os.path.expanduser(args.dest))
    dbs = sorted(src_dir.glob("*.db"))
    if not dbs:
        print(f"no .db files in {src_dir}")
        return 1

    if not args.dry_run:
        fp = register_qp_key()
        if fp:
            print(f"registered qp signing key (fingerprint {fp}) in the keyring\n")

    totals = {"imported": 0, "skipped": 0, "empty": 0, "aborted": 0, "capsules": 0}
    for db in dbs:
        status, n, fails = import_chain(db, dest_dir / db.name, args.dry_run)
        if status.startswith("imported") or status.startswith("would"):
            totals["imported"] += 1
            totals["capsules"] += n
        elif status.startswith("skip"):
            totals["skipped"] += 1
            totals["capsules"] += n
        elif status.startswith("empty"):
            totals["empty"] += 1
        elif status.startswith("ABORT"):
            totals["aborted"] += 1
        print(f"  {db.stem[:18]:18}  {n:5d} caps  {status}")

    print(
        f"\n{totals['imported']} imported, {totals['skipped']} skipped, "
        f"{totals['empty']} empty, {totals['aborted']} aborted "
        f"({totals['capsules']} capsules total)"
    )

    if args.seal_meta and not totals["aborted"]:
        print()
        seal_meta_chain(dest_dir, reset=True)

    return 1 if totals["aborted"] else 0


if __name__ == "__main__":
    sys.exit(main())
