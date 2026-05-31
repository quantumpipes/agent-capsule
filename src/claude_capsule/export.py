# SPDX-License-Identifier: Apache-2.0
"""Export capsule chains to the static JSON bundle the Explorer SPA verifies.

Reads the SQLite chain DBs directly (column reads, no deserialization), and
writes one tiny ``index.json`` (chain summaries + the Ed25519 public key) plus
one ``<chain-id>.json`` per chain carrying each capsule's seal fields and the
exact canonical bytes. The browser re-derives every display field, the SHA3-256
hash, and the signature from that, fully offline.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .seal import Seal

DEFAULT_GLOBS = ["~/.claude-capsule/chains/*.db"]


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute("SELECT * FROM capsules ORDER BY sequence ASC")
        return list(cur.fetchall())
    finally:
        conn.close()


def _export_capsule(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "hash": row["hash"],
        "signature": row["signature"],
        "signature_pq": row["signature_pq"],
        "signed_at": row["signed_at"],
        "signed_by": row["signed_by"],
        "canonical": row["canonical"],
    }


def _hash_ok(cap: dict[str, Any]) -> bool:
    return hashlib.sha3_256(cap["canonical"].encode("utf-8")).hexdigest() == cap["hash"]


def _title(capsules: list[dict[str, Any]], fallback: str) -> str:
    for c in capsules:
        try:
            req = (json.loads(c["canonical"]).get("trigger") or {}).get("request") or ""
        except (json.JSONDecodeError, AttributeError):
            req = ""
        if req:
            return req.strip().split("\n")[0][:90]
    return fallback


def export_chains(db_paths: list[Path], out_dir: Path, public_key: str, fingerprint: str) -> dict:
    chains: list[dict[str, Any]] = []
    for db in db_paths:
        rows = _rows(db)
        if not rows:
            continue
        capsules = [_export_capsule(r) for r in rows]
        chains.append({
            "id": db.stem,
            "db_path": str(db),
            "title": _title(capsules, db.stem),
            "length": len(capsules),
            "head_hash": capsules[-1]["hash"],
            "genesis_hash": capsules[0]["hash"],
            "all_hashes_ok": all(_hash_ok(c) for c in capsules),
            "capsules": capsules,
        })
    chains.sort(key=lambda c: c["length"], reverse=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for ch in chains:
        (out_dir / f"{ch['id']}.json").write_text(json.dumps(ch, ensure_ascii=False))
        summaries.append({k: ch[k] for k in
                          ("id", "title", "length", "head_hash", "genesis_hash", "all_hashes_ok")})

    index = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "public_key": public_key,
        "fingerprint": fingerprint,
        "chain_count": len(chains),
        "capsule_count": sum(c["length"] for c in chains),
        "chains": summaries,
    }
    (out_dir / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export capsule chains to a static JSON bundle")
    ap.add_argument("--db", action="append", default=[], help="explicit SQLite DB path (repeatable)")
    ap.add_argument("--glob", action="append", default=[], help="glob for DB files (repeatable)")
    ap.add_argument("--out", required=True, help="output dir for index.json + per-chain files")
    args = ap.parse_args(argv)

    patterns = args.glob or DEFAULT_GLOBS
    db_paths = [Path(os.path.expanduser(p)) for p in args.db]
    for pat in patterns:
        db_paths += [Path(p) for p in glob.glob(os.path.expanduser(pat))]
    seen: set[str] = set()
    db_paths = [p for p in db_paths if p.exists() and str(p) not in seen and not seen.add(str(p))]

    seal = Seal()
    try:
        public_key, fingerprint = seal.get_public_key(), seal.get_key_fingerprint()
    except Exception:
        public_key, fingerprint = "", ""

    index = export_chains(db_paths, Path(os.path.expanduser(args.out)), public_key, fingerprint)
    print(f"exported {index['chain_count']} chain(s), {index['capsule_count']} capsule(s) -> {args.out}/")
    print(f"public_key={public_key[:16]}... fingerprint={fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
