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

from ..core.capsule import TYPE_CHAT, TYPE_SYSTEM, TYPE_TOOL
from ..core.sealing import SUMMARY_CAP, seal_specs
from ..core.sealing import log as _seal_log
from ..core.sealing import trunc as _trunc

TOOL = "claude-code"
FILE_WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    _seal_log(TOOL, msg)


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
# Seal (capsule construction + chaining are shared in core.sealing)
# --------------------------------------------------------------------------- #
def run(session_id: str, transcript_path: str, finalize: bool) -> None:
    plan = build_plan(load_records(transcript_path))
    shared = os.environ.get("AGENT_CAPSULE_DB")
    if shared:
        seal_specs(TOOL, session_id, plan, finalize=finalize,
                   db_path=Path(os.path.expanduser(shared)), tenant_id=session_id)
    else:
        seal_specs(TOOL, session_id, plan, finalize=finalize)


HOOK_COMMAND = "agent-capsule-claude-hook"
SETTINGS = Path(os.path.expanduser("~/.claude/settings.json"))


def install() -> None:
    """Register the Stop + SessionEnd hooks in ~/.claude/settings.json (idempotent)."""
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if SETTINGS.exists():
        try:
            data = json.loads(SETTINGS.read_text())
        except Exception as e:
            print(f"refusing to modify {SETTINGS}: not valid JSON ({e})")
            return
    hooks = data.setdefault("hooks", {})
    entry = {"type": "command", "command": HOOK_COMMAND}
    for event in ("Stop", "SessionEnd"):
        arr = hooks.setdefault(event, [])
        present = any(h.get("command") == HOOK_COMMAND
                      for g in arr for h in g.get("hooks", []))
        if present:
            print(f"   {event}: already registered")
        else:
            arr.append({"hooks": [entry]})
            print(f"   {event}: added")
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
    print(f"claude-code hooks registered in {SETTINGS}")


def uninstall() -> None:
    """Remove only our entries from ~/.claude/settings.json."""
    if not SETTINGS.exists():
        return
    try:
        data = json.loads(SETTINGS.read_text())
    except Exception:
        return
    hooks = data.get("hooks", {})
    for event in ("Stop", "SessionEnd"):
        arr = hooks.get(event)
        if not arr:
            continue
        arr[:] = [g for g in arr
                  if not any(h.get("command") == HOOK_COMMAND for h in g.get("hooks", []))]
        if not arr:
            hooks.pop(event, None)
    SETTINGS.write_text(json.dumps(data, indent=2) + "\n")
    print(f"claude-code hooks removed from {SETTINGS}")


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
