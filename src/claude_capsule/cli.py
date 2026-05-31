# SPDX-License-Identifier: Apache-2.0
"""claude-capsule command line: verify, inspect, and export capsule chains.

  claude-capsule verify  <chain.db>        recompute hashes + signatures, report breaks
  claude-capsule inspect <chain.db> [--seq N]   print capsules (or one)
  claude-capsule export  --out DIR         write the static JSON bundle for the explorer
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .chain import CapsuleChain
from .export import main as export_main
from .seal import Seal
from .storage import CapsuleStorage


def _cmd_verify(args: argparse.Namespace) -> int:
    storage = CapsuleStorage(Path(os.path.expanduser(args.db)))
    try:
        seal = Seal() if args.signatures else None
        result = CapsuleChain(storage).verify(seal=seal)
        rows = storage.get_all_ordered()
        mark = "OK" if result.valid else "BROKEN"
        print(f"[{mark}] {args.db}: {result.capsules_verified}/{len(rows)} verified", end="")
        if not result.valid:
            print(f" (broken at seq {result.broken_at}: {result.error})")
            return 1
        print(f" (head {rows[-1]['hash'][:16] if rows else 'genesis'})")
        return 0
    finally:
        storage.close()


def _cmd_inspect(args: argparse.Namespace) -> int:
    storage = CapsuleStorage(Path(os.path.expanduser(args.db)))
    try:
        rows = storage.get_all_ordered()
        if args.seq is not None:
            rows = [r for r in rows if r["sequence"] == args.seq]
            if not rows:
                print(f"no capsule at sequence {args.seq}", file=sys.stderr)
                return 1
        for r in rows:
            d = json.loads(r["canonical"])
            ctype = str(d.get("type", "?"))
            req = (d.get("trigger") or {}).get("request", "")
            line = req.strip().split("\n")[0][:70]
            print(f"#{r['sequence']:>3} {ctype:<7} {r['hash'][:12]} {line}")
            if args.seq is not None:
                print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0
    finally:
        storage.close()


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # `export` forwards all remaining flags straight to the exporter, so parse it
    # off before argparse so its --out/--db/--glob aren't seen as unknown here.
    if argv and argv[0] == "export":
        return export_main(argv[1:])

    ap = argparse.ArgumentParser(prog="claude-capsule", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="verify a chain's hashes and links")
    v.add_argument("db")
    v.add_argument("--signatures", action="store_true", help="also verify Ed25519 signatures")
    v.set_defaults(func=_cmd_verify)

    i = sub.add_parser("inspect", help="list capsules, or print one with --seq")
    i.add_argument("db")
    i.add_argument("--seq", type=int, default=None)
    i.set_defaults(func=_cmd_inspect)

    sub.add_parser("export", help="write the static JSON bundle for the explorer "
                                  "(args: --out DIR [--db PATH] [--glob PAT])")

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
