#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Export capsule chains to the static JSON bundle this explorer verifies.

Self-contained: reads the SQLite chain DBs written by the claude-capsule hook
and emits public/data/chains/{index.json, <chain-id>.json}. Each capsule carries
its seal fields plus the exact canonical bytes, so the browser re-derives every
display field, the SHA3-256 hash, and the Ed25519 signature, fully offline.

Only dependency beyond the stdlib is PyNaCl, used to read the public key from
~/.claude-capsule/key so the bundle can ship it.

Usage:
  python3 scripts/export_chains.py                       # default discovery
  python3 scripts/export_chains.py --db PATH [--db PATH]
  python3 scripts/export_chains.py --glob '~/.claude-capsule/chains/*.db'
  python3 scripts/export_chains.py --out public/data/chains
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

DEFAULT_GLOBS = ["~/.claude-capsule/chains/*.db"]
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR.parent / "public" / "data" / "chains"
KEY_PATH = Path(os.path.expanduser("~/.claude-capsule/key"))


def public_key_hex() -> tuple[str, str]:
    try:
        from nacl.signing import SigningKey
        sk = SigningKey(KEY_PATH.read_bytes())
        pk = sk.verify_key.encode().hex()
        return pk, pk[:16]
    except Exception:
        return "", ""


def rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM capsules ORDER BY sequence ASC").fetchall())
    finally:
        conn.close()


def export_capsule(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "hash": r["hash"], "signature": r["signature"], "signature_pq": r["signature_pq"],
        "signed_at": r["signed_at"], "signed_by": r["signed_by"], "canonical": r["canonical"],
    }


def hash_ok(c: dict[str, Any]) -> bool:
    return hashlib.sha3_256(c["canonical"].encode("utf-8")).hexdigest() == c["hash"]


def title(capsules: list[dict[str, Any]], fallback: str) -> str:
    for c in capsules:
        try:
            req = (json.loads(c["canonical"]).get("trigger") or {}).get("request") or ""
        except (json.JSONDecodeError, AttributeError):
            req = ""
        if req:
            return req.strip().split("\n")[0][:90]
    return fallback


def main() -> int:
    ap = argparse.ArgumentParser(description="Export capsule chains to a static JSON bundle")
    ap.add_argument("--db", action="append", default=[])
    ap.add_argument("--glob", action="append", default=[])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    patterns = args.glob or DEFAULT_GLOBS
    db_paths = [Path(os.path.expanduser(p)) for p in args.db]
    for pat in patterns:
        db_paths += [Path(p) for p in glob.glob(os.path.expanduser(pat))]
    seen: set[str] = set()
    db_paths = [p for p in db_paths if p.exists() and str(p) not in seen and not seen.add(str(p))]

    pk, fp = public_key_hex()
    chains: list[dict[str, Any]] = []
    for db in db_paths:
        rs = rows(db)
        if not rs:
            continue
        capsules = [export_capsule(r) for r in rs]
        chains.append({
            "id": db.stem, "db_path": str(db), "title": title(capsules, db.stem),
            "length": len(capsules), "head_hash": capsules[-1]["hash"],
            "genesis_hash": capsules[0]["hash"], "all_hashes_ok": all(hash_ok(c) for c in capsules),
            "capsules": capsules,
        })
    chains.sort(key=lambda c: c["length"], reverse=True)

    out_dir = Path(os.path.expanduser(args.out))
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for ch in chains:
        (out_dir / f"{ch['id']}.json").write_text(json.dumps(ch, ensure_ascii=False))
        summaries.append({k: ch[k] for k in
                          ("id", "title", "length", "head_hash", "genesis_hash", "all_hashes_ok")})
    index = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "public_key": pk, "fingerprint": fp, "chain_count": len(chains),
        "capsule_count": sum(c["length"] for c in chains), "chains": summaries,
    }
    (out_dir / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    print(f"exported {len(chains)} chain(s), {index['capsule_count']} capsule(s) -> {out_dir}/")
    print(f"public_key={pk[:16]}... fingerprint={fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
