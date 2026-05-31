# SPDX-License-Identifier: Apache-2.0
"""Turn a Cline task into a tamper-evident capsule hashchain.

Wired as Cline hook scripts (``~/Documents/Cline/Hooks/TaskComplete`` /
``TaskCancel`` / ``TaskStart``, and project ``.clinerules/hooks/*``). Cline runs
each script as a child process, passes a JSON payload on **stdin** (containing
the hook name, the ``taskId`` and ``cwd``), and reads a JSON object on stdout.
For sealing we only observe: we print ``{}`` (never cancel).

Hooks are TRIGGERS; the per-task folder JSON is the source of truth. Cline keeps
each task under VS Code's globalStorage for publisher ``saoudrizwan.claude-dev``:

    <base>/tasks/<taskId>/
        ui_messages.json              ClineMessage[] (the durable spine, by ts)
        api_conversation_history.json Anthropic MessageParam[]
        task_metadata.json            files_in_context / model_usage / env

We parse ``ui_messages.json`` ordered by ``ts`` into one capsule per action
(tool call) and one per assistant text/completion, then append them to a
per-task hashchain. ``session_id = taskId``.

The hook is fail-open: any error is logged and the process exits 0 (and still
prints ``{}``), so it can never block or cancel a Cline task. Because the shim
execs this same ``main`` for every hook type and ``seal_specs`` is idempotent,
we simply finalize the task on every invocation.

Standalone usage (testing without registering the hook):
  python -m agent_capsule.adapters.cline --task-dir <path> --session <taskId> [--finalize]
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import traceback
from pathlib import Path
from typing import Any

from ..core.capsule import TYPE_CHAT, TYPE_SYSTEM, TYPE_TOOL
from ..core.sealing import SUMMARY_CAP, seal_specs
from ..core.sealing import log as _seal_log
from ..core.sealing import trunc as _trunc

TOOL = "cline"

# ClineSayTool.tool values that mutate the filesystem.
FILE_WRITE_TOOLS = {"editedExistingFile", "newFileCreated", "fileDeleted", "appliedDiff"}

# Known VS Code-family globalStorage bases for the Cline publisher.
_PUBLISHER = "saoudrizwan.claude-dev"
_APP_DIRS = ("Code", "Cursor", "VSCodium", "Code - Insiders")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    _seal_log(TOOL, msg)


def globalstorage_bases() -> list[Path]:
    """Candidate globalStorage dirs for the Cline publisher across editors/OSes."""
    home = Path(os.path.expanduser("~"))
    roots: list[Path] = []
    # macOS
    roots += [home / "Library" / "Application Support" / app for app in _APP_DIRS]
    # Linux
    roots += [home / ".config" / app for app in _APP_DIRS]
    # Windows
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots += [Path(appdata) / app for app in _APP_DIRS]
    return [r / "User" / "globalStorage" / _PUBLISHER for r in roots]


def find_task_dir(task_id: str) -> Path | None:
    """Find ``tasks/<taskId>/`` in the first globalStorage base that has it."""
    for base in globalstorage_bases():
        d = base / "tasks" / task_id
        if d.is_dir():
            return d
    return None


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _maybe_json(text: Any) -> Any:
    """Many ClineMessage ``text`` fields hold a JSON string; decode if so."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _short(text: Any, cap: int = 200) -> str:
    return str(text or "").replace("\n", " ").strip()[:cap]


# --------------------------------------------------------------------------- #
# task metadata -> model / env
# --------------------------------------------------------------------------- #
def task_model(meta: dict[str, Any]) -> str:
    usage = meta.get("model_usage") or []
    if isinstance(usage, list) and usage:
        last = usage[-1]
        if isinstance(last, dict):
            return str(last.get("model_id") or "")
    return ""


def task_env(meta: dict[str, Any]) -> dict[str, Any]:
    env: dict[str, Any] = {}
    hist = meta.get("environment_history") or []
    if isinstance(hist, list) and hist:
        last = hist[-1]
        if isinstance(last, dict):
            env["os_name"] = last.get("os_name", "")
            env["cline_version"] = last.get("cline_version", "")
    return env


# --------------------------------------------------------------------------- #
# ui_messages.json -> capsule plan
# --------------------------------------------------------------------------- #
def build_plan(messages: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk ClineMessage[] (ordered by ts) into a list of capsule specs."""
    model = task_model(meta)
    env = task_env(meta)
    msgs = [m for m in messages if isinstance(m, dict)]
    msgs.sort(key=lambda m: m.get("ts", 0))

    plan: list[dict[str, Any]] = []
    current_prompt = ""
    pending_usage: dict[str, Any] = {}
    pending_thinking = ""

    def base(ts: Any, index: int, *, narrative: str = "", thinking: str = "",
             authority: str = "autonomous") -> dict[str, Any]:
        spec = {
            "key": f"{ts}:{index}",
            "prompt": current_prompt,
            "agent_id": TOOL,
            "env": dict(env),
            "model": model,
            "narrative": narrative,
            "thinking": thinking,
            "authority_type": authority,
        }
        return spec

    def take_usage() -> dict[str, Any]:
        nonlocal pending_usage
        u, pending_usage = pending_usage, {}
        return u

    def take_thinking() -> str:
        nonlocal pending_thinking
        t, pending_thinking = pending_thinking, ""
        return t

    for index, m in enumerate(msgs):
        ts = m.get("ts", index)
        mtype = m.get("type")
        say = m.get("say")
        ask = m.get("ask")
        text = m.get("text")
        reasoning = m.get("reasoning")

        if reasoning:
            pending_thinking = (pending_thinking + "\n" + str(reasoning)).strip()

        # ---- user request / prompt ----------------------------------------
        if say in ("task", "user_feedback") or ask == "followup":
            current_prompt = str(text or current_prompt)
            if say in ("task", "user_feedback") and text:
                plan.append({
                    **base(ts, index),
                    "type": TYPE_CHAT,
                    "tool": None,
                    "summary": f"user: {_short(text)}",
                    "status": "success",
                    "response": "",
                    "structured": {"role": "user", "say": say or ask, "text": _trunc(str(text), SUMMARY_CAP)},
                })
            continue

        # ---- token usage carried onto the next action ----------------------
        if say == "api_req_started":
            info = _maybe_json(text) or {}
            if isinstance(info, dict):
                pending_usage = {
                    "input_tokens": info.get("tokensIn"),
                    "output_tokens": info.get("tokensOut"),
                    "cache_writes": info.get("cacheWrites"),
                    "cache_reads": info.get("cacheReads"),
                    "cost": info.get("cost"),
                }
            continue

        # ---- reasoning (hidden thinking) -----------------------------------
        if say == "reasoning":
            if text:
                pending_thinking = (pending_thinking + "\n" + str(text)).strip()
            continue

        # ---- tool call (ClineSayTool JSON in text) -------------------------
        if say == "tool" or ask == "tool":
            info = _maybe_json(text) or {}
            tname = str(info.get("tool") or "tool")
            path = info.get("path")
            args = {k: info.get(k) for k in ("path", "diff", "content", "regex")
                    if info.get(k) is not None}
            side: list[str] = []
            if tname in FILE_WRITE_TOOLS and path:
                side.append(f"wrote {path}")
            summary = f"{tname}: {path}" if path else tname
            # ask:"tool" means Cline requested human approval before acting.
            authority = "human_approved" if ask == "tool" else "autonomous"
            plan.append({
                **base(ts, index, thinking=take_thinking(), authority=authority),
                "type": TYPE_TOOL,
                "usage": take_usage(),
                "tool": tname,
                "arguments": args,
                "result": None,
                "side_effects": side,
                "success": True,
                "summary": summary,
                "status": "success",
            })
            continue

        # ---- shell command + its output -----------------------------------
        if say == "command" or ask == "command":
            authority = "human_approved" if ask == "command" else "autonomous"
            plan.append({
                **base(ts, index, thinking=take_thinking(), authority=authority),
                "type": TYPE_TOOL,
                "usage": take_usage(),
                "tool": "command",
                "arguments": {"command": str(text or "")},
                "result": None,
                "side_effects": [f"ran {_short(text)}"],
                "success": True,
                "summary": f"command: {_short(text)}",
                "status": "success",
            })
            continue

        if say == "command_output":
            # Attach output to the most recent command tool spec.
            for spec in reversed(plan):
                if spec.get("tool") == "command":
                    prior = spec.get("result")
                    spec["result"] = (str(prior) + str(text or "")) if prior else str(text or "")
                    break
            continue

        # ---- browser / MCP tool actions ------------------------------------
        if say == "browser_action" or ask == "use_mcp_server" or say == "mcp_server_response":
            info = _maybe_json(text)
            if say == "browser_action":
                tname = "browser_action"
            elif ask == "use_mcp_server":
                tname = "use_mcp_server"
            else:
                tname = "mcp_server_response"
            plan.append({
                **base(ts, index, thinking=take_thinking()),
                "type": TYPE_TOOL,
                "usage": take_usage(),
                "tool": tname,
                "arguments": info if isinstance(info, dict) else {"text": _short(text, 1000)},
                "result": None,
                "summary": f"{tname}: {_short(text, 120)}",
                "status": "success",
            })
            continue

        # ---- assistant prose / completion ----------------------------------
        if say in ("text", "completion_result") or ask == "completion_result":
            narrative = str(text or "")
            if not narrative and not pending_thinking:
                continue
            plan.append({
                **base(ts, index, narrative=narrative, thinking=take_thinking()),
                "type": TYPE_CHAT,
                "usage": take_usage(),
                "tool": None,
                "summary": _trunc(narrative, SUMMARY_CAP) or "assistant reasoning",
                "status": "success",
                "response": narrative,
            })
            continue

    # If we accumulated usage/thinking with no action to carry it, drop a
    # small system capsule so nothing is silently lost.
    if pending_usage or pending_thinking:
        plan.append({
            "key": f"trailing:{len(msgs)}",
            "type": TYPE_SYSTEM,
            "prompt": current_prompt,
            "agent_id": TOOL,
            "env": dict(env),
            "model": model,
            "narrative": "",
            "thinking": pending_thinking,
            "authority_type": "autonomous",
            "tool": None,
            "usage": pending_usage,
            "summary": "trailing usage/reasoning",
            "status": "success",
            "response": "",
        })

    return plan


# --------------------------------------------------------------------------- #
# Seal
# --------------------------------------------------------------------------- #
def run(session_id: str, task_dir: Path, finalize: bool) -> dict[str, Any]:
    messages = _read_json(task_dir / "ui_messages.json", [])
    meta = _read_json(task_dir / "task_metadata.json", {})
    if not isinstance(messages, list):
        messages = []
    if not isinstance(meta, dict):
        meta = {}
    plan = build_plan(messages, meta)
    shared = os.environ.get("AGENT_CAPSULE_DB")
    if shared:
        return seal_specs(TOOL, session_id, plan, finalize=finalize,
                          db_path=Path(os.path.expanduser(shared)), tenant_id=session_id)
    return seal_specs(TOOL, session_id, plan, finalize=finalize)


# --------------------------------------------------------------------------- #
# install / uninstall (hook shim scripts)
# --------------------------------------------------------------------------- #
HOOK_NAMES = ("TaskComplete", "TaskCancel", "TaskStart")
_HOOKS_DIR = Path(os.path.expanduser("~")) / "Documents" / "Cline" / "Hooks"
_POSIX_SHIM = "#!/bin/sh\nexec agent-capsule-cline-hook\n"
_PS1_SHIM = "& agent-capsule-cline-hook\n"


def install() -> None:
    """Write executable Cline hook shims that exec this adapter's ``main``.

    Writes ``~/Documents/Cline/Hooks/{TaskComplete,TaskCancel,TaskStart}`` POSIX
    scripts (chmod 0755) plus ``.ps1`` variants for Windows. Idempotent: only our
    own shim files are overwritten, never unrelated files in the directory.
    """
    _HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    mode = (stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0755
    for name in HOOK_NAMES:
        posix = _HOOKS_DIR / name
        posix.write_text(_POSIX_SHIM, encoding="utf-8")
        try:
            os.chmod(posix, mode)
        except OSError as e:
            _log(f"chmod {posix} failed: {e}")
        ps1 = _HOOKS_DIR / f"{name}.ps1"
        ps1.write_text(_PS1_SHIM, encoding="utf-8")
    _log(f"installed hooks in {_HOOKS_DIR}")


def uninstall() -> None:
    """Remove only the three shim files (and their .ps1) we created."""
    for name in HOOK_NAMES:
        for p in (_HOOKS_DIR / name, _HOOKS_DIR / f"{name}.ps1"):
            try:
                if p.exists():
                    p.unlink()
            except OSError as e:
                _log(f"uninstall {p} failed: {e}")
    _log(f"uninstalled hooks from {_HOOKS_DIR}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    """Fail-open hook entry. Reads hook JSON from stdin (prints ``{}``) or runs
    in offline test mode via ``--task-dir``. Always returns 0."""
    ap = argparse.ArgumentParser(description="Cline task -> capsule hashchain")
    ap.add_argument("--task-dir", help="path to tasks/<taskId>/ (else resolve from taskId)")
    ap.add_argument("--session", help="taskId (else from hook JSON on stdin)")
    ap.add_argument("--finalize", action="store_true", help="verify chain after appending")
    args = ap.parse_args(argv)

    session_id = args.session
    task_dir = Path(args.task_dir) if args.task_dir else None
    finalize = args.finalize
    stdin_mode = task_dir is None

    try:
        if stdin_mode:
            event: dict[str, Any] = {}
            try:
                event = json.load(sys.stdin)
            except Exception as e:
                _log(f"no stdin JSON and no --task-dir: {e}")
                event = {}
            if isinstance(event, dict):
                session_id = (session_id or event.get("taskId")
                              or event.get("task_id") or event.get("session_id"))
            # The shim execs us for every hook type; seal_specs is idempotent,
            # so we always finalize the task on any observational invocation.
            finalize = True
            if session_id:
                task_dir = find_task_dir(str(session_id))
                if task_dir is None:
                    _log(f"task dir not found for taskId={session_id!r}")
            else:
                _log("no taskId in hook payload")

        if task_dir is not None and session_id and task_dir.is_dir():
            run(str(session_id), task_dir, finalize)
        elif task_dir is not None:
            _log(f"task dir missing or no session: dir={task_dir!r} session={session_id!r}")
    except Exception:
        _log("ERROR\n" + traceback.format_exc())
    finally:
        if stdin_mode:
            # Observe-only: never cancel the Cline task.
            try:
                sys.stdout.write("{}")
                sys.stdout.flush()
            except Exception:
                pass

    return 0  # always fail-open


if __name__ == "__main__":
    sys.exit(main())
