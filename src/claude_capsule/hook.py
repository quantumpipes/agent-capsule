# SPDX-License-Identifier: Apache-2.0
"""Turn a Claude Code conversation into a tamper-evident capsule hashchain.

Wired as a Claude Code hook (Stop + SessionEnd). On each fire it reads the
session transcript JSONL, builds one capsule per agent action (tool call) and
one per text response, and appends them to a per-conversation hashchain.

Hooks are TRIGGERS; the transcript is the source of truth. We parse the
transcript rather than the stdin event.

Claude Code REDACTS extended-thinking text in the stored transcript (only a
cryptographic ``signature`` survives). We capture the next best thing: the
thinking signatures as proof-of-reasoning, carried onto the action they
preceded, plus a ``thinking_redacted`` flag.

Storage (first match wins):
  CLAUDE_CAPSULE_DB=<path>  -> one shared SQLite file (all sessions, one chain
                               grouped by session_id as tenant_id)
  (default)                 -> ~/.claude-capsule/chains/{session_id}.db
                               one file per conversation = one independent chain.
                               Traverse with:  claude-capsule verify <file>

The hook is fail-open: any error is logged and the process exits 0, so it can
never block or stall a Claude Code session.

Standalone usage (testing without registering the hook):
  python -m claude_capsule.hook --transcript <path.jsonl> --session <id> [--finalize]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .capsule import TYPE_CHAT, TYPE_SYSTEM, TYPE_TOOL, Capsule
from .chain import CapsuleChain
from .seal import Seal
from .storage import CapsuleStorage

FULL = None  # sentinel: no truncation
RESULT_CAP = 200_000
SUMMARY_CAP = 280

HOME = Path(os.path.expanduser("~/.claude-capsule"))
CHAIN_DIR = HOME / "chains"
LOG_PATH = HOME / "hook.log"

# permission modes where the agent acts without a human approval gate
AUTONOMOUS_MODES = {"bypassPermissions", "acceptEdits", "dontAsk", "auto"}
FILE_WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} {msg}\n")
    except Exception:
        pass


def _trunc(value: Any, limit: int | None = RESULT_CAP) -> Any:
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


def tool_summary(tool: str, args: Any) -> str:
    a = args if isinstance(args, dict) else {}
    target = (
        a.get("command") or a.get("file_path") or a.get("path") or a.get("pattern")
        or a.get("query") or a.get("url") or a.get("description") or a.get("prompt")
        or a.get("skill") or ""
    )
    target = str(target).replace("\n", " ").strip()
    return f"{tool}: {target[:200]}" if target else f"{tool} call"


def _block_text(blocks: list[dict[str, Any]], kind: str) -> str:
    out: list[str] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == kind:
            out.append(str(b.get(kind if kind == "thinking" else "text", "")))
    return "\n".join(t for t in out if t).strip()


def _ts_ms(a: str | None, b: str | None) -> int | None:
    try:
        if not a or not b:
            return None
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return max(0, int((tb - ta).total_seconds() * 1000))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Transcript -> capsule plan
# --------------------------------------------------------------------------- #
def load_records(transcript_path: str) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    with open(transcript_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def index_tool_results(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.get("type") != "user":
            continue
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    out[tid] = {
                        "content": b.get("content"),
                        "is_error": bool(b.get("is_error")),
                        "ts": r.get("timestamp"),
                        "structured": r.get("toolUseResult"),
                    }
    return out


def first_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return str(b.get("text", ""))
    return ""


def build_plan(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = index_tool_results(records)
    plan: list[dict[str, Any]] = []
    current_prompt = ""
    current_mode = ""
    pending_think: dict[str, Any] = {"count": 0, "sigs": []}

    def drain_think() -> dict[str, Any]:
        out = {
            "thinking_blocks": pending_think["count"],
            "thinking_signatures": list(pending_think["sigs"]),
            "thinking_redacted": pending_think["count"] > 0,
        }
        pending_think["count"] = 0
        pending_think["sigs"] = []
        return out

    for r in records:
        rtype = r.get("type")
        is_side = bool(r.get("isSidechain"))

        if r.get("permissionMode"):
            current_mode = str(r.get("permissionMode"))
        elif rtype in ("permission-mode", "mode") and (r.get("mode") or r.get("permission_mode")):
            current_mode = str(r.get("mode") or r.get("permission_mode"))

        if rtype == "attachment" and r.get("attachment"):
            plan.append({
                "key": f"{r.get('uuid', '')}:attach",
                "type": TYPE_SYSTEM,
                "prompt": current_prompt,
                "agent_id": "claude-code",
                "env": {"cwd": r.get("cwd", ""), "timestamp": r.get("timestamp", ""),
                        "message_uuid": r.get("uuid", "")},
                "thinking": "", "narrative": "", "model": "", "permission_mode": current_mode,
                "tool": None,
                "summary": f"attachment: {(r['attachment'] or {}).get('type', 'context')}",
                "status": "success", "response": "", "structured": r.get("attachment"),
            })
            continue

        if rtype == "user" and r.get("toolUseResult") is None and not r.get("isMeta"):
            msg = r.get("message") or {}
            txt = first_text(msg.get("content"))
            if txt and not (isinstance(msg.get("content"), list)
                            and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                    for b in msg["content"])):
                current_prompt = txt
            continue

        if rtype != "assistant":
            continue

        msg = r.get("message") or {}
        blocks = msg.get("content") or []
        if not isinstance(blocks, list):
            continue

        model = msg.get("model", "")
        usage = msg.get("usage") or {}
        thinking = _block_text(blocks, "thinking")
        narrative = _block_text(blocks, "text")
        uuid = r.get("uuid", "")
        a_ts = r.get("timestamp")
        agent_id = "claude-code-subagent" if is_side else "claude-code"

        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "thinking":
                pending_think["count"] += 1
                if b.get("signature"):
                    pending_think["sigs"].append(str(b["signature"])[:24])
        images = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image")

        tool_uses = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not tool_uses and not (narrative or thinking):
            continue

        think_prov = drain_think()
        env = {
            "cwd": r.get("cwd", ""), "git_branch": r.get("gitBranch", ""),
            "cc_version": r.get("version", ""), "model": model, "is_sidechain": is_side,
            "request_id": r.get("requestId", ""), "prompt_id": r.get("promptId", ""),
            "parent_uuid": r.get("parentUuid", ""), "message_uuid": uuid,
            "timestamp": a_ts or "", "permission_mode": current_mode,
            "user_type": r.get("userType", ""), "stop_reason": msg.get("stop_reason", ""),
            "attribution_skill": r.get("attributionSkill", ""), "images": images, **think_prov,
        }
        base = {
            "prompt": current_prompt, "agent_id": agent_id, "env": env,
            "thinking": thinking, "narrative": narrative, "model": model,
            "permission_mode": current_mode,
        }

        if tool_uses:
            for i, tu in enumerate(tool_uses):
                tid = tu.get("id", "")
                res = results.get(tid, {})
                ok = not res.get("is_error", False)
                tool = tu.get("name", "?")
                args = tu.get("input", {})
                side: list[str] = []
                if tool in FILE_WRITE_TOOLS and isinstance(args, dict) and args.get("file_path"):
                    side.append(f"wrote {args['file_path']}")
                plan.append({
                    **base, "key": f"{uuid}:{i}", "type": TYPE_TOOL,
                    "usage": usage if i == 0 else {}, "tool": tool, "tool_id": tid,
                    "arguments": args, "result": res.get("content"),
                    "structured": res.get("structured"), "side_effects": side, "success": ok,
                    "duration_ms": _ts_ms(a_ts, res.get("ts")),
                    "summary": tool_summary(tool, args) + (" (error)" if not ok else ""),
                    "status": "success" if ok else "failure",
                })
        elif narrative or thinking:
            plan.append({
                **base, "key": f"{uuid}:resp", "type": TYPE_CHAT, "usage": usage, "tool": None,
                "summary": _trunc(narrative or thinking, SUMMARY_CAP),
                "status": "success", "response": narrative,
            })

    return plan


# --------------------------------------------------------------------------- #
# Capsule construction + chain append
# --------------------------------------------------------------------------- #
def make_capsule(spec: dict[str, Any], session_id: str) -> Capsule:
    reasoning = {
        "analysis": _trunc(spec.get("narrative", ""), FULL),
        "reasoning": _trunc(spec.get("thinking", ""), FULL),
        "model": spec.get("model", ""),
    }
    execution: dict[str, Any] = {"tool_calls": [], "duration_ms": 0, "resources_used": {}}
    if spec.get("tool"):
        execution["tool_calls"] = [{
            "tool": spec["tool"],
            "arguments": _trunc(spec.get("arguments", {}), FULL),
            "result": _trunc(spec.get("result"), RESULT_CAP),
            "success": bool(spec.get("success", True)),
            "duration_ms": spec.get("duration_ms"),
            "error": None if spec.get("success", True) else "tool reported is_error",
        }]
    usage = spec.get("usage") or {}
    if usage:
        execution["resources_used"] = _trunc(dict(usage), FULL)

    mode = spec.get("permission_mode") or ""
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
        outcome["result"] = _trunc(spec["response"], FULL)
    elif spec.get("structured") is not None:
        outcome["result"] = _trunc(spec["structured"], RESULT_CAP)
    if not spec.get("success", True):
        outcome["error"] = _trunc(spec.get("result"), RESULT_CAP)

    return Capsule(
        type=spec["type"],
        domain="claude-code",
        trigger={"type": "user_request", "source": session_id,
                 "request": _trunc(spec.get("prompt", ""), FULL)},
        context={"agent_id": spec.get("agent_id", "claude-code"),
                 "session_id": session_id, "environment": spec.get("env", {})},
        reasoning=reasoning,
        authority=authority,
        execution=execution,
        outcome=outcome,
    )


def resolve_storage(session_id: str) -> tuple[CapsuleStorage, str | None, str]:
    shared = os.environ.get("CLAUDE_CAPSULE_DB")
    if shared:
        return CapsuleStorage(Path(os.path.expanduser(shared))), session_id, f"sqlite-shared {shared}"
    CHAIN_DIR.mkdir(parents=True, exist_ok=True)
    db = CHAIN_DIR / f"{session_id}.db"
    return CapsuleStorage(db), None, f"sqlite-per-session {db}"


def checkpoint_path(session_id: str) -> Path:
    CHAIN_DIR.mkdir(parents=True, exist_ok=True)
    return CHAIN_DIR / f"{session_id}.checkpoint.json"


def load_done(session_id: str) -> set[str]:
    p = checkpoint_path(session_id)
    if p.exists():
        try:
            return set(json.loads(p.read_text()).get("done", []))
        except Exception:
            return set()
    return set()


def save_done(session_id: str, done: set[str]) -> None:
    try:
        checkpoint_path(session_id).write_text(json.dumps({"done": sorted(done)}))
    except Exception as e:
        _log(f"checkpoint write failed: {e}")


def run(session_id: str, transcript_path: str, finalize: bool) -> None:
    records = load_records(transcript_path)
    plan = build_plan(records)
    done = load_done(session_id)
    pending = [s for s in plan if s["key"] not in done]

    storage, tenant_id, label = resolve_storage(session_id)
    chain = CapsuleChain(storage)
    seal = Seal()

    appended = 0
    try:
        for spec in pending:
            cap = make_capsule(spec, session_id)
            chain.seal_and_store(cap, seal=seal, tenant_id=tenant_id)
            done.add(spec["key"])
            appended += 1
        save_done(session_id, done)

        if finalize:
            result = chain.verify(tenant_id=tenant_id, seal=seal)
            rows = storage.get_all_ordered(tenant_id=tenant_id)
            head_hash = rows[-1]["hash"] if rows else ""
            _log(f"finalize session={session_id} [{label}] appended={appended} "
                 f"verify(valid={result.valid}, verified={result.capsules_verified}, "
                 f"broken_at={result.broken_at}) head={head_hash[:12]} capsules={len(rows)}")
        else:
            _log(f"append session={session_id} [{label}] +{appended} (total plan={len(plan)})")
    finally:
        storage.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Claude Code conversation -> capsule hashchain")
    ap.add_argument("--transcript", help="transcript JSONL path (else read hook JSON from stdin)")
    ap.add_argument("--session", help="session id (else from hook JSON / transcript filename)")
    ap.add_argument("--finalize", action="store_true", help="verify chain after appending")
    args = ap.parse_args(argv)

    session_id = args.session
    transcript = args.transcript
    finalize = args.finalize

    if not transcript:
        try:
            event = json.load(sys.stdin)
        except Exception as e:
            _log(f"no stdin JSON and no --transcript: {e}")
            return 0
        session_id = session_id or event.get("session_id")
        transcript = event.get("transcript_path")
        if event.get("hook_event_name") == "SessionEnd":
            finalize = True

    if not transcript or not Path(transcript).exists():
        _log(f"transcript missing: {transcript!r}")
        return 0
    if not session_id:
        session_id = Path(transcript).stem

    try:
        run(session_id, transcript, finalize)
    except Exception:
        _log("ERROR\n" + traceback.format_exc())
    return 0  # always fail-open


if __name__ == "__main__":
    sys.exit(main())
