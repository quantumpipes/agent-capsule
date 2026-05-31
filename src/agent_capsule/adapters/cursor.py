# SPDX-License-Identifier: Apache-2.0
"""Turn a Cursor agent conversation into a tamper-evident capsule hashchain.

Wired as a Cursor hook (``stop`` + ``sessionEnd``) via ``~/.cursor/hooks.json``.
On each fire Cursor sends a small JSON payload on stdin and expects ``{}`` back
on stdout; the hook is observe-only, so it never blocks the agent.

The hook payload is a TRIGGER. The source of truth is Cursor's own state:

  1. Preferred: the globalStorage SQLite store
     (``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``,
     table ``cursorDiskKV``). It holds ``composerData:<conversation_id>`` (the
     ordered list of message bubbles) and one ``bubbleId:<conversation_id>:<id>``
     row per message, including ``toolFormerData`` for tool calls. This is the
     only place tool calls, arguments, results and token counts live. The DB is
     hot (WAL, held open by Cursor), so we copy it (plus -wal/-shm) to a temp dir
     and open the copy read-only.

  2. Fallback: the plain transcript JSONL at ``transcript_path`` (text only, no
     tool calls). User prompts are wrapped in ``<user_query>...</user_query>``.

Either way we produce a list of tool-agnostic capsule specs and hand them to the
shared ``seal_specs`` (all hashing, signing, chaining, idempotency live there).

The hook is fail-open: any error is logged and the process exits 0.

Standalone usage (testing without Cursor installed):
  python -m agent_capsule.adapters.cursor --db <state.vscdb> \
      --transcript <path.jsonl> --session <conversation_id> [--finalize]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from ..core.capsule import TYPE_CHAT, TYPE_TOOL
from ..core.sealing import SUMMARY_CAP, seal_specs
from ..core.sealing import log as _seal_log
from ..core.sealing import trunc as _trunc

TOOL = "cursor"

HOOK_COMMAND = "agent-capsule-cursor-hook"
HOOKS_JSON = Path(os.path.expanduser("~/.cursor/hooks.json"))
HOOK_EVENTS = ("stop", "sessionEnd")
FINALIZE_STATES = {"completed", "aborted", "error"}

# Bubble "type" discriminators in cursorDiskKV.
BUBBLE_USER = 1
BUBBLE_ASSISTANT = 2


def _log(msg: str) -> None:
    _seal_log(TOOL, msg)


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #
def default_vscdb() -> Path:
    """Default path to Cursor's globalStorage state DB (macOS)."""
    return Path(os.path.expanduser(
        "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    ))


def strip_user_query(text: str) -> str:
    """Strip the ``<user_query>...</user_query>`` wrapper Cursor adds to prompts."""
    t = (text or "").strip()
    if t.startswith("<user_query>") and t.endswith("</user_query>"):
        t = t[len("<user_query>"):-len("</user_query>")]
    else:
        t = t.replace("<user_query>", "").replace("</user_query>", "")
    return t.strip()


def _maybe_json(value: Any) -> Any:
    """Parse a JSON string if it looks like one, else return it unchanged."""
    if isinstance(value, str):
        s = value.strip()
        if s and s[0] in "{[":
            try:
                return json.loads(s)
            except (ValueError, TypeError):
                return value
    return value


def tool_summary(name: str, args: Any) -> str:
    """One-line ``name: <target>`` summary from tool params."""
    a = args if isinstance(args, dict) else {}
    target = (
        a.get("command") or a.get("file") or a.get("file_path") or a.get("path")
        or a.get("relativeWorkspacePath") or a.get("query") or a.get("pattern")
        or a.get("url") or a.get("target_file") or ""
    )
    target = str(target).replace("\n", " ").strip()
    return f"{name}: {target[:200]}" if target else f"{name} call"


# --------------------------------------------------------------------------- #
# SQLite (globalStorage state.vscdb) path
# --------------------------------------------------------------------------- #
def _safe_copy(db_path: Path, dest_dir: str) -> Path:
    """Copy the live DB plus its -wal/-shm siblings into a temp dir."""
    copy = Path(dest_dir) / db_path.name
    shutil.copy2(db_path, copy)
    for suffix in ("-wal", "-shm"):
        sib = db_path.with_name(db_path.name + suffix)
        if sib.exists():
            try:
                shutil.copy2(sib, Path(dest_dir) / sib.name)
            except OSError:
                pass
    return copy


def _kv_get(conn: sqlite3.Connection, key: str) -> Any:
    cur = conn.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        return None
    value = row[0]
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    return _maybe_json(value)


def build_specs_from_db(db_path: Path, conversation_id: str,
                        base_env: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk composerData + bubbles into capsule specs. Empty list = unavailable."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []

    tmp = tempfile.mkdtemp(prefix="agent-capsule-cursor-")
    try:
        copy = _safe_copy(db_path, tmp)
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            composer = _kv_get(conn, f"composerData:{conversation_id}")
            if not isinstance(composer, dict):
                return []

            headers = composer.get("fullConversationHeadersOnly") or []
            if not isinstance(headers, list):
                return []

            conv_model = (composer.get("modelConfig") or {}).get("modelName", "")
            env_common = {**base_env}
            if composer.get("name"):
                env_common["title"] = composer.get("name")
            if composer.get("createdAt") is not None:
                env_common["conversation_created_at_ms"] = composer.get("createdAt")
            if composer.get("contextTokensUsed") is not None:
                env_common["context_tokens_used"] = composer.get("contextTokensUsed")

            specs: list[dict[str, Any]] = []
            current_prompt = ""

            for header in headers:
                if not isinstance(header, dict):
                    continue
                bubble_id = header.get("bubbleId")
                if not bubble_id:
                    continue
                bubble = _kv_get(conn, f"bubbleId:{conversation_id}:{bubble_id}")
                if not isinstance(bubble, dict):
                    continue

                btype = bubble.get("type")
                text = bubble.get("text") or ""
                created_at = bubble.get("createdAt")
                token_count = bubble.get("tokenCount") or {}
                model = bubble.get("modelName") or conv_model or env_common.get("model", "")

                usage: dict[str, Any] = {}
                if isinstance(token_count, dict) and token_count:
                    if token_count.get("inputTokens") is not None:
                        usage["input_tokens"] = token_count.get("inputTokens")
                    if token_count.get("outputTokens") is not None:
                        usage["output_tokens"] = token_count.get("outputTokens")

                env = {**env_common, "model": model, "bubble_id": bubble_id}
                if created_at is not None:
                    env["created_at"] = created_at

                tfd = bubble.get("toolFormerData")
                if isinstance(tfd, dict) and tfd:
                    name = tfd.get("name") or "tool"
                    args = tfd.get("params")
                    if not isinstance(args, dict):
                        args = _maybe_json(tfd.get("rawArgs"))
                    result = _maybe_json(tfd.get("result"))
                    status = (tfd.get("status") or "").lower()
                    add_status = ((tfd.get("additionalData") or {}).get("status") or "").lower() \
                        if isinstance(tfd.get("additionalData"), dict) else ""
                    success = True
                    if add_status:
                        success = add_status != "error"
                    elif status:
                        success = status not in {"error", "failed", "cancelled", "aborted"}
                    specs.append({
                        "key": str(bubble_id),
                        "type": TYPE_TOOL,
                        "prompt": current_prompt,
                        "agent_id": TOOL,
                        "env": env,
                        "model": model,
                        "authority_type": "autonomous",
                        "tool": name,
                        "arguments": args if args is not None else {},
                        "result": result,
                        "success": success,
                        "usage": usage,
                        "summary": tool_summary(name, args) + ("" if success else " (error)"),
                        "status": "success" if success else "failure",
                    })
                elif btype == BUBBLE_ASSISTANT and text.strip():
                    specs.append({
                        "key": str(bubble_id),
                        "type": TYPE_CHAT,
                        "prompt": current_prompt,
                        "agent_id": TOOL,
                        "env": env,
                        "model": model,
                        "authority_type": "autonomous",
                        "narrative": text,
                        "usage": usage,
                        "summary": _trunc(text, SUMMARY_CAP),
                        "status": "success",
                        "response": text,
                    })
                elif btype == BUBBLE_USER and text.strip():
                    current_prompt = strip_user_query(text)

            return specs
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        _log(f"sqlite read failed: {e}")
        return []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Transcript JSONL fallback
# --------------------------------------------------------------------------- #
def _line_text(obj: dict[str, Any]) -> str:
    message = obj.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                str(b.get("text", "")) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p).strip()
    if isinstance(message, str):
        return message
    return ""


def build_specs_from_transcript(transcript_path: str,
                                base_env: dict[str, Any]) -> list[dict[str, Any]]:
    """Text-only fallback: user lines set the prompt, assistant lines are chats."""
    path = Path(transcript_path)
    if not path.exists():
        return []

    specs: list[dict[str, Any]] = []
    current_prompt = ""
    try:
        with path.open(encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                role = obj.get("role")
                text = _line_text(obj)
                if role == "user" and text.strip():
                    current_prompt = strip_user_query(text)
                elif role == "assistant" and text.strip():
                    specs.append({
                        "key": f"line:{idx}",
                        "type": TYPE_CHAT,
                        "prompt": current_prompt,
                        "agent_id": TOOL,
                        "env": {**base_env, "transcript_line": idx},
                        "model": base_env.get("model", ""),
                        "authority_type": "autonomous",
                        "narrative": text,
                        "summary": _trunc(text, SUMMARY_CAP),
                        "status": "success",
                        "response": text,
                    })
    except OSError as e:
        _log(f"transcript read failed: {e}")
        return []
    return specs


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(conversation_id: str, *, db_path: Path | None, transcript_path: str | None,
        base_env: dict[str, Any], finalize: bool) -> dict[str, Any]:
    """Build specs (DB preferred, transcript fallback) and seal them."""
    specs: list[dict[str, Any]] = []
    if db_path is not None:
        specs = build_specs_from_db(db_path, conversation_id, base_env)
    if not specs and transcript_path:
        specs = build_specs_from_transcript(transcript_path, base_env)

    shared = os.environ.get("AGENT_CAPSULE_DB")
    if shared:
        return seal_specs(TOOL, conversation_id, specs, finalize=finalize,
                          db_path=Path(os.path.expanduser(shared)), tenant_id=conversation_id)
    return seal_specs(TOOL, conversation_id, specs, finalize=finalize)


def main(argv: list[str] | None = None) -> int:
    """Fail-open entry point. As a hook: read stdin JSON, print ``{}``, exit 0."""
    try:
        ap = argparse.ArgumentParser(description="Cursor conversation -> capsule hashchain")
        ap.add_argument("--transcript", help="transcript JSONL path")
        ap.add_argument("--db", help="globalStorage state.vscdb path")
        ap.add_argument("--session", help="conversation id")
        ap.add_argument("--finalize", action="store_true", help="verify chain after appending")
        args = ap.parse_args(argv)

        from_stdin = not (args.transcript or args.db or args.session)
        event: dict[str, Any] = {}
        if from_stdin:
            try:
                event = json.load(sys.stdin)
            except Exception as e:  # noqa: BLE001 - fail open on any stdin issue
                _log(f"no stdin JSON: {e}")
                event = {}

        conversation_id = args.session or event.get("conversation_id")
        transcript_path = args.transcript or event.get("transcript_path")
        status = event.get("status", "")

        db_path: Path | None
        if args.db:
            db_path = Path(os.path.expanduser(args.db))
        elif from_stdin:
            db_path = default_vscdb()
        else:
            db_path = None

        base_env = {
            "model": event.get("model", ""),
            "cursor_version": event.get("cursor_version", ""),
            "workspace_roots": event.get("workspace_roots", []),
            "user_email": event.get("user_email", ""),
            "hook_event": event.get("hook_event_name", ""),
            "status": status,
        }
        base_env = {k: v for k, v in base_env.items() if v not in ("", [], None)}

        finalize = bool(args.finalize) or status in FINALIZE_STATES \
            or event.get("hook_event_name") == "sessionEnd"

        if not conversation_id:
            if transcript_path:
                conversation_id = Path(transcript_path).stem
            else:
                _log("no conversation_id and no transcript; nothing to seal")
                if from_stdin:
                    sys.stdout.write("{}")
                return 0

        try:
            run(conversation_id, db_path=db_path, transcript_path=transcript_path,
                base_env=base_env, finalize=finalize)
        except Exception:  # noqa: BLE001 - fail open
            _log("ERROR\n" + traceback.format_exc())
    except Exception:  # noqa: BLE001 - absolute fail open
        try:
            _log("FATAL\n" + traceback.format_exc())
        except Exception:
            pass

    # Hooks observe by returning {} on stdout.
    if argv is None or not (argv and ("--transcript" in argv or "--db" in argv or "--session" in argv)):
        try:
            sys.stdout.write("{}")
        except Exception:
            pass
    return 0


# --------------------------------------------------------------------------- #
# install() / uninstall() (wired into the CLI elsewhere)
# --------------------------------------------------------------------------- #
def _read_hooks_json() -> dict[str, Any]:
    if HOOKS_JSON.exists():
        try:
            data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def _write_hooks_json(data: dict[str, Any]) -> None:
    HOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def install(global_: bool = True) -> None:
    """Register the cursor hook in ``~/.cursor/hooks.json`` (idempotent).

    Adds ``{"command": HOOK_COMMAND, "type": "command"}`` under ``hooks.stop`` and
    ``hooks.sessionEnd`` without removing existing hooks or duplicating ours.
    Preserves ``version: 1``.
    """
    data = _read_hooks_json()
    data.setdefault("version", 1)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    entry = {"command": HOOK_COMMAND, "type": "command"}
    for event in HOOK_EVENTS:
        existing = hooks.get(event)
        if not isinstance(existing, list):
            existing = []
            hooks[event] = existing
        if not any(isinstance(h, dict) and h.get("command") == HOOK_COMMAND for h in existing):
            existing.append(dict(entry))

    _write_hooks_json(data)


def uninstall() -> None:
    """Remove only our entries from ``~/.cursor/hooks.json`` (leave the rest)."""
    if not HOOKS_JSON.exists():
        return
    data = _read_hooks_json()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in HOOK_EVENTS:
        existing = hooks.get(event)
        if isinstance(existing, list):
            kept = [h for h in existing
                    if not (isinstance(h, dict) and h.get("command") == HOOK_COMMAND)]
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
    _write_hooks_json(data)


if __name__ == "__main__":
    sys.exit(main())
