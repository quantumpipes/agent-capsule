# SPDX-License-Identifier: Apache-2.0
"""agent-capsule command line: verify, inspect, list, and export capsule chains.

  agent-capsule verify  <chain.db> [--signatures]   recompute hashes + signatures
  agent-capsule inspect <chain.db> [--seq N]         print capsules (or one)
  agent-capsule list                                 list every chain, grouped by tool
  agent-capsule export  --out DIR                    write the static bundle for the explorer
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import ModuleType

from .core.chain import CapsuleChain
from .core.export import main as export_main
from .core.paths import CHAINS_DIR
from .core.seal import Seal
from .core.storage import CapsuleStorage


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


def _cmd_verify_meta(args: argparse.Namespace) -> int:
    from .core.meta import verify_meta
    r = verify_meta(seal=Seal() if args.signatures else None, deep=args.deep)
    mark = "OK" if r.valid else "BROKEN"
    print(f"[{mark}] meta-chain: {r.meta_capsules} capsules, "
          f"{r.conversations_checked} conversations checked "
          f"(head {r.meta_head[:16] or 'genesis'})")
    if r.error:
        print(f"  {r.error}")
    for f in r.failures:
        print(f"  - {f}")
    return 0 if r.valid else 1


def _cmd_list(_args: argparse.Namespace) -> int:
    if not CHAINS_DIR.exists():
        print(f"no chains yet ({CHAINS_DIR} does not exist)")
        return 0
    found = False
    for tool_dir in sorted(p for p in CHAINS_DIR.iterdir() if p.is_dir()):
        dbs = sorted(tool_dir.glob("*.db"))
        if not dbs:
            continue
        found = True
        print(f"{tool_dir.name}/")
        for db in dbs:
            storage = CapsuleStorage(db)
            try:
                n = len(storage.get_all_ordered())
            finally:
                storage.close()
            print(f"  {db.stem:<40} {n:>4} capsules")
    if not found:
        print(f"no chains yet (looked in {CHAINS_DIR})")
    return 0


_ADAPTERS = {"claude-code": "claude_code", "cursor": "cursor", "codex": "codex", "cline": "cline"}


def _adapter(tool: str) -> ModuleType:
    import importlib
    return importlib.import_module(f"agent_capsule.adapters.{_ADAPTERS[tool]}")


def _cmd_install(args: argparse.Namespace) -> int:
    print(f"Installing the {args.tool} capsule hook...")
    _adapter(args.tool).install()
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    _adapter(args.tool).uninstall()
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    # `export` forwards all remaining flags straight to the exporter, so parse it
    # off before argparse so its --out/--db/--glob aren't seen as unknown here.
    if argv and argv[0] == "export":
        return export_main(argv[1:])

    ap = argparse.ArgumentParser(prog="agent-capsule", description=__doc__,
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

    vm = sub.add_parser("verify-meta", help="verify the meta-chain and every conversation head")
    vm.add_argument("--signatures", action="store_true", help="also verify Ed25519 signatures")
    vm.add_argument("--deep", action="store_true", help="also fully re-verify each conversation")
    vm.set_defaults(func=_cmd_verify_meta)

    le = sub.add_parser("list", help="list every chain, grouped by tool")
    le.set_defaults(func=_cmd_list)

    ins = sub.add_parser("install", help="register the capsule hook for a tool")
    ins.add_argument("tool", choices=list(_ADAPTERS))
    ins.set_defaults(func=_cmd_install)

    uni = sub.add_parser("uninstall", help="remove the capsule hook for a tool")
    uni.add_argument("tool", choices=list(_ADAPTERS))
    uni.set_defaults(func=_cmd_uninstall)

    sub.add_parser("export", help="write the static JSON bundle for the explorer "
                                  "(args: --out DIR [--db PATH] [--glob PAT])")

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
