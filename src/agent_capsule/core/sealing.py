# SPDX-License-Identifier: Apache-2.0
"""Shared sealing: turn a list of capsule specs into a sealed, chained DB.

Every adapter produces the SAME spec shape (a plain dict per action) and calls
``seal_specs``. All the hashing, signing, chaining, idempotency, and fail-open
logging live here, so an adapter is nothing but a parser.

A spec dict (all keys optional except ``type`` and ``key``):

    key            str   idempotency key, unique per action within a session
    type           str   "tool" | "chat" | "system" | "agent"
    prompt         str   the user request that drove this action
    agent_id       str   defaults to the tool name
    env            dict  provenance (cwd, model, timestamps, ...)
    narrative      str   visible assistant prose (-> reasoning.analysis)
    thinking       str   hidden reasoning text, usually "" (-> reasoning.reasoning)
    model          str
    permission_mode str  tool's permission/approval mode (drives authority)
    authority_type str   explicit "autonomous"|"policy"|"human_approved" (overrides above)
    policy_reference str
    tool           str   tool name, if this action is a tool call
    arguments      any   tool input
    result         any   tool output
    success        bool  default True
    duration_ms    int
    usage          dict  token usage (-> execution.resources_used)
    summary        str   one-line outcome summary
    side_effects   list[str]
    status         str   "success" | "failure"
    response       str   assistant response text (for chat capsules)
    structured     any   extra structured payload (-> outcome.result)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .capsule import Capsule
from .chain import CapsuleChain
from .paths import HOME, LOG_PATH, checkpoint_file, chain_db
from .seal import Seal
from .storage import CapsuleStorage

FULL = None  # sentinel: no truncation
RESULT_CAP = 200_000
SUMMARY_CAP = 280

# permission/approval modes where the agent acted without a human gate
AUTONOMOUS_MODES = {"bypassPermissions", "acceptEdits", "dontAsk", "auto", "yolo", "full-auto", "auto-edit"}


def log(tool: str, msg: str) -> None:
    """Append a line to the shared fail-open adapter log. Never raises."""
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} [{tool}] {msg}\n")
    except Exception:
        pass


def trunc(value: Any, limit: int | None = RESULT_CAP) -> Any:
    """Truncate large strings/objects. limit=None (FULL) keeps verbatim."""
    if limit is None:
        return value
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"...<+{len(value) - limit}b>"
    if isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False, default=str)
        if len(s) <= limit:
            return value
        return {"_truncated": s[:limit] + f"...<+{len(s) - limit}b>"}
    return value


def make_capsule(tool: str, session_id: str, spec: dict[str, Any]) -> Capsule:
    """Build a six-section Capsule from a tool-agnostic spec dict."""
    reasoning = {
        "analysis": trunc(spec.get("narrative", ""), FULL),
        "reasoning": trunc(spec.get("thinking", ""), FULL),
        "model": spec.get("model", ""),
    }

    execution: dict[str, Any] = {"tool_calls": [], "duration_ms": 0, "resources_used": {}}
    if spec.get("tool"):
        execution["tool_calls"] = [{
            "tool": spec["tool"],
            "arguments": trunc(spec.get("arguments", {}), FULL),
            "result": trunc(spec.get("result"), RESULT_CAP),
            "success": bool(spec.get("success", True)),
            "duration_ms": spec.get("duration_ms"),
            "error": None if spec.get("success", True) else "tool reported an error",
        }]
    usage = spec.get("usage") or {}
    if usage:
        execution["resources_used"] = trunc(dict(usage), FULL)

    explicit = spec.get("authority_type")
    mode = spec.get("permission_mode") or ""
    if explicit:
        authority = {"type": explicit, "policy_reference": spec.get("policy_reference", "")}
    else:
        authority = {
            "type": "autonomous" if (not mode or mode in AUTONOMOUS_MODES) else "policy",
            "policy_reference": f"permission_mode={mode}" if mode else "",
        }

    outcome: dict[str, Any] = {
        "status": spec.get("status", "success"),
        "summary": spec.get("summary", ""),
        "side_effects": spec.get("side_effects", []) or [],
    }
    if spec.get("response"):
        outcome["result"] = trunc(spec["response"], FULL)
    elif spec.get("structured") is not None:
        outcome["result"] = trunc(spec["structured"], RESULT_CAP)
    if not spec.get("success", True) and spec.get("result") is not None:
        outcome["error"] = trunc(spec.get("result"), RESULT_CAP)

    return Capsule(
        type=spec["type"],
        domain=tool,
        trigger={"type": "user_request", "source": session_id,
                 "request": trunc(spec.get("prompt", ""), FULL)},
        context={"agent_id": spec.get("agent_id") or tool,
                 "session_id": session_id, "environment": spec.get("env", {})},
        reasoning=reasoning,
        authority=authority,
        execution=execution,
        outcome=outcome,
    )


def _load_done(tool: str, session_id: str) -> set[str]:
    p = checkpoint_file(tool, session_id)
    if p.exists():
        try:
            return set(json.loads(p.read_text()).get("done", []))
        except Exception:
            return set()
    return set()


def _save_done(tool: str, session_id: str, done: set[str]) -> None:
    try:
        checkpoint_file(tool, session_id).write_text(json.dumps({"done": sorted(done)}))
    except Exception as e:
        log(tool, f"checkpoint write failed: {e}")


def seal_specs(
    tool: str,
    session_id: str,
    specs: list[dict[str, Any]],
    *,
    finalize: bool = False,
    db_path: Path | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Seal any specs not already sealed for this session, idempotently.

    Repeated calls (e.g. one per turn) append only new actions, tracked by each
    spec's ``key`` in a per-session checkpoint. Returns a small status dict.

    Pass ``db_path``/``tenant_id`` to override the default per-session DB (used
    by the shared-store mode).
    """
    storage = CapsuleStorage(db_path or chain_db(tool, session_id))
    chain = CapsuleChain(storage)
    seal = Seal()
    done = _load_done(tool, session_id)
    pending = [s for s in specs if s.get("key") not in done]

    appended = 0
    result: dict[str, Any] = {}
    try:
        for spec in pending:
            chain.seal_and_store(make_capsule(tool, session_id, spec), seal=seal, tenant_id=tenant_id)
            if spec.get("key"):
                done.add(spec["key"])
            appended += 1
        _save_done(tool, session_id, done)

        rows = storage.get_all_ordered(tenant_id=tenant_id)
        result = {"appended": appended, "total": len(rows),
                  "head": rows[-1]["hash"] if rows else ""}
        if finalize:
            v = chain.verify(tenant_id=tenant_id, seal=seal)
            result.update(valid=v.valid, verified=v.capsules_verified, broken_at=v.broken_at)
            if v.valid and rows:
                try:
                    from .meta import record_conversation
                    result["meta"] = record_conversation(
                        tool, session_id, result["head"], len(rows),
                        seal=seal, db_path=db_path, tenant_id=tenant_id)
                except Exception as e:  # fail-open: never break the agent's hook
                    log(tool, f"meta record failed (non-fatal): {e}")
            log(tool, f"finalize session={session_id} appended={appended} total={len(rows)} "
                      f"valid={v.valid} broken_at={v.broken_at} head={result['head'][:12]}")
        else:
            log(tool, f"append session={session_id} +{appended} total={len(rows)}")
        return result
    finally:
        storage.close()


# Convenience for adapters that read a hook payload from stdin.
def read_stdin_json() -> dict[str, Any]:
    import sys
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


HookMain = Callable[..., int]
