# SPDX-License-Identifier: Apache-2.0
"""The meta-chain: a chain of conversation seals.

When a conversation chain is finalized, its head hash (which transitively
commits to every capsule in that conversation) and capsule count are appended
as one capsule to a single, machine-wide meta-chain. The meta-chain is itself a
hash chain, so its head is one value that commits to every conversation sealed.

This closes two gaps a per-conversation chain cannot close alone: deleting a
whole conversation (its head is referenced here but the chain is gone) and
truncating a conversation's tail (its recomputed head no longer matches the
head recorded here). Appends are serialized with a file lock because concurrent
sessions may finalize at once.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .chain import CapsuleChain
from .paths import HOME, META_CHECKPOINT, META_DB, META_LOCK, chain_db
from .seal import Seal
from .storage import CapsuleStorage

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

SEAL_KIND = "conversation_seal"


@contextlib.contextmanager
def _meta_lock():
    """Serialize meta-chain appends across concurrent sessions."""
    if fcntl is None:
        yield
        return
    HOME.mkdir(parents=True, exist_ok=True)
    fh = META_LOCK.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _load_done() -> set[str]:
    if META_CHECKPOINT.exists():
        try:
            return set(json.loads(META_CHECKPOINT.read_text()).get("done", []))
        except Exception:
            return set()
    return set()


def _save_done(done: set[str]) -> None:
    META_CHECKPOINT.write_text(json.dumps({"done": sorted(done)}))


def record_conversation(
    tool: str,
    session_id: str,
    head_hash: str,
    capsule_count: int,
    *,
    seal: Seal | None = None,
    db_path: Path | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Append one conversation-seal capsule to the meta-chain, idempotently.

    Keyed by (tool, session_id, head_hash): re-finalizing an unchanged
    conversation is a no-op; a conversation that grew (new head) appends a new
    entry, and the latest entry per conversation is authoritative on verify.

    Args:
        tool: The agent tool the conversation belongs to (e.g. "claude-code").
        session_id: The conversation/session identifier.
        head_hash: Hash of the conversation chain's last capsule.
        capsule_count: Number of capsules in the conversation chain.
        seal: Seal to sign the meta capsule with; a default Seal is used if None.
        db_path: Conversation DB location when not the default per-session path.
        tenant_id: Conversation tenant when using the shared-store mode.

    Returns:
        A status dict: either {"recorded": False, "reason": ...} when the head
        was already recorded, or {"recorded": True, "meta_head", "meta_total"}.
    """
    seal = seal or Seal()
    key = f"{tool}:{session_id}:{head_hash}"
    payload = {
        "kind": SEAL_KIND,
        "tool": tool,
        "session_id": session_id,
        "head_hash": head_hash,
        "capsule_count": capsule_count,
    }
    if db_path is not None:
        payload["db"] = str(db_path)
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id

    from .sealing import make_capsule  # lazy: sealing imports us back

    spec = {
        "key": key,
        "type": "system",
        "agent_id": "agent-capsule",
        "prompt": f"conversation sealed: {tool}/{session_id}",
        "summary": f"{tool}/{session_id}: {capsule_count} capsules, head {head_hash[:12]}",
        "env": {"tool": tool, "session_id": session_id},
        "structured": payload,
    }

    with _meta_lock():
        done = _load_done()
        if key in done:
            return {"recorded": False, "reason": "already recorded"}
        storage = CapsuleStorage(META_DB)
        try:
            CapsuleChain(storage).seal_and_store(make_capsule("meta", "meta", spec), seal=seal)
            rows = storage.get_all_ordered()
        finally:
            storage.close()
        done.add(key)
        _save_done(done)
    return {"recorded": True, "meta_head": rows[-1]["hash"], "meta_total": len(rows)}


@dataclass
class MetaVerificationResult:
    valid: bool
    conversations_checked: int = 0
    meta_capsules: int = 0
    meta_head: str = ""
    error: str | None = None
    failures: list[str] = field(default_factory=list)


def _seal_payloads(rows) -> dict[tuple[str, str], dict]:
    """Latest conversation-seal payload per (tool, session_id)."""
    latest: dict[tuple[str, str], dict] = {}
    for r in rows:
        result = (json.loads(r["canonical"]).get("outcome") or {}).get("result") or {}
        if isinstance(result, dict) and result.get("kind") == SEAL_KIND:
            latest[(result["tool"], result["session_id"])] = result
    return latest


def verify_meta(seal: Seal | None = None, *, deep: bool = False) -> MetaVerificationResult:
    """Verify the meta-chain, then cross-check every recorded conversation head.

    Catches a tampered meta-chain (links/hashes/signatures), a deleted
    conversation (DB gone), and a truncated/altered conversation (its recomputed
    head or count no longer matches what was recorded). With ``deep``, also fully
    re-verifies each conversation chain internally.

    Args:
        seal: Seal whose public key checks signatures; None skips signature checks.
        deep: When True, fully re-verify each referenced conversation chain.

    Returns:
        A MetaVerificationResult; ``valid`` is False if the meta-chain is broken
        or any conversation fails its head/count cross-check.
    """
    storage = CapsuleStorage(META_DB)
    try:
        meta = CapsuleChain(storage).verify(seal=seal)
        rows = storage.get_all_ordered()
    finally:
        storage.close()

    if not meta.valid:
        return MetaVerificationResult(
            valid=False,
            meta_capsules=len(rows),
            error=f"meta-chain broken at seq {meta.broken_at}: {meta.error}",
        )

    payloads = _seal_payloads(rows)
    failures: list[str] = []
    for (tool, sid), p in payloads.items():
        db = Path(p["db"]) if p.get("db") else chain_db(tool, sid)
        tenant = p.get("tenant_id")
        if not db.exists():
            failures.append(f"{tool}/{sid}: conversation deleted (no {db})")
            continue
        cs = CapsuleStorage(db)
        try:
            crows = cs.get_all_ordered(tenant_id=tenant)
            inner = CapsuleChain(cs).verify(tenant_id=tenant, seal=seal) if deep else None
        finally:
            cs.close()
        actual_head = crows[-1]["hash"] if crows else ""
        if len(crows) != p["capsule_count"] or actual_head != p["head_hash"]:
            failures.append(
                f"{tool}/{sid}: expected {p['capsule_count']} caps / head "
                f"{p['head_hash'][:12]}, found {len(crows)} / {actual_head[:12]}"
            )
        elif inner is not None and not inner.valid:
            failures.append(f"{tool}/{sid}: internal chain broken at seq {inner.broken_at}")

    return MetaVerificationResult(
        valid=not failures,
        conversations_checked=len(payloads),
        meta_capsules=len(rows),
        meta_head=rows[-1]["hash"] if rows else "",
        failures=failures,
    )
