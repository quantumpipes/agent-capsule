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
from ..core.sealing import RESULT_CAP, SUMMARY_CAP, seal_specs
from ..core.sealing import log as _seal_log
from ..core.sealing import trunc as _trunc

TOOL = "cline"

# ClineSayTool.tool values that mutate the filesystem (the cline `tool` enum
# verb subset that writes), paired with the human-readable side-effect verb.
FILE_WRITE_VERBS = {
    "editedExistingFile": "edited",
    "newFileCreated": "wrote",
    "fileDeleted": "deleted",
    "appliedDiff": "edited",
}
FILE_WRITE_TOOLS = set(FILE_WRITE_VERBS)

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


def _looks_like_diff(text: str) -> bool:
    """A cline replace_in_file diff uses SEARCH/REPLACE markers or hunk headers."""
    return ("<<<<<<< SEARCH" in text or "------- SEARCH" in text
            or "@@ " in text or text.lstrip().startswith(("---", "+++")))


def _count_diff(text: str) -> tuple[int, int]:
    """Count added/removed lines in a unified or SEARCH/REPLACE diff."""
    added = removed = 0
    for ln in text.splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            removed += 1
    return added, removed


# --------------------------------------------------------------------------- #
# api_conversation_history.json -> ordered tool results + thinking
# --------------------------------------------------------------------------- #
def _content_text(content: Any) -> Any:
    """A tool_result `content` is a string or a list of blocks; flatten to text.

    Returns the original string, the joined text of text-blocks, or (when there
    is no text) the structured list so nothing is silently lost.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        if parts:
            return "\n".join(p for p in parts if p)
        return content
    return content


def parse_api_history(history: list[Any]) -> tuple[list[Any], list[str]]:
    """Walk the Anthropic MessageParam[] history once.

    Returns ``(results, thinkings)`` where ``results[i]`` is the truncated
    tool_result content paired (by ``tool_use_id``) with the i-th ``tool_use``
    block in document order, and ``thinkings`` is the per-tool_use accumulated
    assistant thinking text that immediately preceded that tool_use (so it can
    be carried onto the matching ui_messages spec).
    """
    # First pass: collect tool_result content keyed by tool_use_id.
    by_id: dict[str, Any] = {}
    for m in history:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid is not None:
                    by_id[str(tid)] = _content_text(b.get("content"))

    # Second pass: tool_use blocks in document order, with the thinking that
    # accumulated since the previous tool_use.
    results: list[Any] = []
    thinkings: list[str] = []
    pending = ""
    for m in history:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "thinking":
                t = str(b.get("thinking", "")).strip()
                if t:
                    pending = (pending + "\n" + t).strip()
            elif btype == "tool_use":
                tid = str(b.get("id", ""))
                results.append(_trunc(by_id.get(tid), RESULT_CAP))
                thinkings.append(pending)
                pending = ""
    return results, thinkings


# --------------------------------------------------------------------------- #
# browser_action_result -> screenshot reference WITHOUT the base64 blob
# --------------------------------------------------------------------------- #
def _strip_screenshot(info: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Turn a browser_action_result payload into a bounded reference.

    The raw ``screenshot`` is a base64 data URI (often hundreds of KB). We store
    its byte length and a flag, never the data. Returns (result, url).
    """
    shot = info.get("screenshot")
    result: dict[str, Any] = {
        "currentUrl": info.get("currentUrl"),
        "logs": _trunc(info.get("logs"), RESULT_CAP),
        "has_screenshot": bool(shot),
        "screenshot_bytes": len(shot) if isinstance(shot, str) else 0,
    }
    return result, str(info.get("currentUrl") or "")


# --------------------------------------------------------------------------- #
# task metadata -> model / env / provenance
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


def provenance_spec(meta: dict[str, Any]) -> dict[str, Any] | None:
    """One leading system capsule summarizing task_metadata.json provenance.

    Captures the files Cline touched (path + source + read/edit dates), the
    per-model usage records, and the os/cline_version history. Keyed stably so
    repeated finalize calls do not duplicate it.
    """
    files = meta.get("files_in_context") or []
    models = meta.get("model_usage") or []
    hist = meta.get("environment_history") or []
    if not (files or models or hist):
        return None

    def _files() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in files:
            if not isinstance(f, dict):
                continue
            out.append({
                "path": f.get("path"),
                "record_source": f.get("record_source"),
                "record_state": f.get("record_state"),
                "cline_read_date": f.get("cline_read_date"),
                "cline_edit_date": f.get("cline_edit_date"),
                "user_edit_date": f.get("user_edit_date"),
            })
        return out

    structured = {
        "files_in_context": _files(),
        "model_usage": [m for m in models if isinstance(m, dict)],
        "environment_history": [h for h in hist if isinstance(h, dict)],
    }
    env = dict(task_env(meta))
    env["files_in_context_count"] = len(structured["files_in_context"])
    env["model_usage_count"] = len(structured["model_usage"])
    return {
        "key": "provenance",
        "type": TYPE_SYSTEM,
        "prompt": "",
        "agent_id": TOOL,
        "env": env,
        "model": task_model(meta),
        "narrative": "",
        "thinking": "",
        "authority_type": "autonomous",
        "tool": None,
        "summary": "session provenance",
        "status": "success",
        "response": "",
        "structured": _trunc(structured, RESULT_CAP),
    }


# --------------------------------------------------------------------------- #
# ui_messages.json -> capsule plan
# --------------------------------------------------------------------------- #
def build_plan(messages: list[dict[str, Any]], meta: dict[str, Any],
               history: list[Any] | None = None) -> list[dict[str, Any]]:
    """Walk ClineMessage[] (ordered by ts) into a list of capsule specs.

    ``history`` is the parsed ``api_conversation_history.json`` (Anthropic
    MessageParam[]). The Nth tool spec emitted from ui_messages corresponds to
    the Nth ``tool_use`` block in that history, so we JOIN them in order to fill
    each tool spec's ``result`` (today None) and carry the preceding assistant
    ``thinking`` onto it.
    """
    model = task_model(meta)
    env = task_env(meta)
    msgs = [m for m in messages if isinstance(m, dict)]
    msgs.sort(key=lambda m: m.get("ts", 0))

    api_results, api_thinkings = parse_api_history(history or [])
    tool_cursor = 0  # index into api_results / api_thinkings, advanced per tool spec

    plan: list[dict[str, Any]] = []
    current_prompt = ""
    pending_usage: dict[str, Any] = {}
    pending_env: dict[str, Any] = {}
    pending_thinking = ""

    def base(ts: Any, index: int, *, narrative: str = "", thinking: str = "",
             authority: str = "autonomous") -> dict[str, Any]:
        nonlocal pending_env
        spec = {
            "key": f"{ts}:{index}",
            "prompt": current_prompt,
            "agent_id": TOOL,
            "env": {**env, **pending_env},
            "model": model,
            "narrative": narrative,
            "thinking": thinking,
            "authority_type": authority,
        }
        pending_env = {}
        return spec

    def take_usage() -> dict[str, Any]:
        nonlocal pending_usage
        u, pending_usage = pending_usage, {}
        return u

    def take_thinking() -> str:
        nonlocal pending_thinking
        t, pending_thinking = pending_thinking, ""
        return t

    def take_api() -> tuple[Any, str]:
        """Pop the next api-history (result, thinking) for the next tool spec."""
        nonlocal tool_cursor
        if tool_cursor < len(api_results):
            res, think = api_results[tool_cursor], api_thinkings[tool_cursor]
            tool_cursor += 1
            return res, think
        return None, ""

    for index, m in enumerate(msgs):
        ts = m.get("ts", index)
        say = m.get("say")
        ask = m.get("ask")
        text = m.get("text")
        reasoning = m.get("reasoning")

        if reasoning:
            pending_thinking = (pending_thinking + "\n" + str(reasoning)).strip()

        # ---- workspace-snapshot + truncation provenance carried onto next --
        # action. Ties a capsule to the checkpoint commit it ran against and to
        # the conversation-history window Cline kept in the model context.
        if m.get("lastCheckpointHash") is not None:
            pending_env["last_checkpoint_hash"] = m.get("lastCheckpointHash")
        if m.get("isCheckpointCheckedOut") is not None:
            pending_env["is_checkpoint_checked_out"] = bool(m.get("isCheckpointCheckedOut"))
        if m.get("conversationHistoryIndex") is not None:
            pending_env["conversation_history_index"] = m.get("conversationHistoryIndex")
        if m.get("conversationHistoryDeletedRange") is not None:
            pending_env["conversation_history_deleted_range"] = m.get("conversationHistoryDeletedRange")

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
                if info.get("cancelReason") is not None:
                    pending_usage["cancel_reason"] = info.get("cancelReason")
                    pending_env["cancel_reason"] = info.get("cancelReason")
                if info.get("streamingFailedMessage"):
                    pending_usage["streaming_failed"] = True
                    pending_env["streaming_failed_message"] = _short(
                        info.get("streamingFailedMessage"), 500)
                if info.get("retryStatus") is not None:
                    pending_env["retry_status"] = info.get("retryStatus")
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
            # Capture the line-range arguments cline records when reading/editing.
            args = {k: info.get(k) for k in (
                "path", "diff", "content", "regex", "filePattern",
                "startLineNumbers", "readLineStart", "readLineEnd",
                "operationIsLocatedInWorkspace") if info.get(k) is not None}
            api_result, api_think = take_api()
            think = (take_thinking() + ("\n" + api_think if api_think else "")).strip()
            side: list[str] = []
            extra = ""
            result: Any = api_result

            # ClineSayTool.content carries `diff || content`: the SEARCH/REPLACE
            # diff for replace_in_file or the full new file for write_to_file.
            content = info.get("diff") or info.get("content") or ""
            content = str(content) if content is not None else ""
            if content and _looks_like_diff(content):
                added, removed = _count_diff(content)
                result = _trunc(content, RESULT_CAP)
                extra = f" (+{added}/-{removed})"
            elif api_result is None and content:
                # write_to_file with a full new file body: keep it as the result.
                result = _trunc(content, RESULT_CAP)

            verb = FILE_WRITE_VERBS.get(tname)
            if verb and path:
                side.append(f"{verb} {path}")
            summary = (f"{tname}: {path}" if path else tname) + extra
            # ask:"tool" means Cline requested human approval before acting.
            authority = "human_approved" if ask == "tool" else "autonomous"
            plan.append({
                **base(ts, index, thinking=think, authority=authority),
                "type": TYPE_TOOL,
                "usage": take_usage(),
                "tool": tname,
                "arguments": args,
                "result": result,
                "side_effects": side,
                "success": True,
                "summary": summary,
                "status": "success",
            })
            continue

        # ---- shell command + its output -----------------------------------
        if say == "command" or ask == "command":
            # Advance the api-history cursor in lockstep (execute_command is a
            # tool_use too) and prefer the api tool_result for the output, with
            # the streamed command_output appended below as a fallback/extra.
            api_result, api_think = take_api()
            think = (take_thinking() + ("\n" + api_think if api_think else "")).strip()
            authority = "human_approved" if ask == "command" else "autonomous"
            plan.append({
                **base(ts, index, thinking=think, authority=authority),
                "type": TYPE_TOOL,
                "usage": take_usage(),
                "tool": "command",
                "arguments": {"command": str(text or "")},
                "result": api_result,
                "side_effects": [f"ran {_short(text)}"],
                "success": True,
                "summary": f"command: {_short(text)}",
                "status": "success",
            })
            continue

        if say == "command_output":
            # Attach streamed output to the most recent command tool spec,
            # appending to any tool_result already filled from api history.
            for spec in reversed(plan):
                if spec.get("tool") == "command":
                    prior = spec.get("result")
                    spec["result"] = _trunc(
                        (str(prior) + str(text or "")) if prior else str(text or ""),
                        RESULT_CAP)
                    break
            continue

        # ---- browser screenshot result (strip the base64 blob) -------------
        if say == "browser_action_result":
            info = _maybe_json(text)
            if isinstance(info, dict):
                result, url = _strip_screenshot(info)
            else:
                result, url = {"has_screenshot": False, "screenshot_bytes": 0}, ""
            side = [f"captured screenshot of {url}"] if result.get("has_screenshot") else []
            # Attach to the most recent browser_action tool spec when present.
            target = None
            for spec in reversed(plan):
                if spec.get("tool") == "browser_action":
                    target = spec
                    break
            if target is not None:
                target["result"] = result
                if side:
                    target["side_effects"] = (target.get("side_effects") or []) + side
                if url:
                    target["summary"] = f"browser_action: {url}"
            else:
                plan.append({
                    **base(ts, index, thinking=take_thinking()),
                    "type": TYPE_TOOL,
                    "usage": take_usage(),
                    "tool": "browser_action_result",
                    "arguments": {},
                    "result": result,
                    "side_effects": side,
                    "summary": f"browser_action_result: {url or '(no url)'}",
                    "status": "success",
                })
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
            # A ClineMessage.images array is raw base64; record the count, not
            # the data, so the capsule never carries an image blob.
            imgs = m.get("images")
            args: dict[str, Any] = info if isinstance(info, dict) else {"text": _short(text, 1000)}
            if isinstance(imgs, list) and imgs:
                args = {**args, "image_count": len(imgs)}
            plan.append({
                **base(ts, index, thinking=take_thinking()),
                "type": TYPE_TOOL,
                "usage": take_usage(),
                "tool": tname,
                "arguments": args,
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

    # Lead with one session-provenance system capsule (files touched, model
    # usage, environment). Keyed stably so finalize re-runs do not duplicate it.
    prov = provenance_spec(meta)
    if prov is not None:
        plan.insert(0, prov)

    return plan


# --------------------------------------------------------------------------- #
# Seal
# --------------------------------------------------------------------------- #
def run(session_id: str, task_dir: Path, finalize: bool) -> dict[str, Any]:
    messages = _read_json(task_dir / "ui_messages.json", [])
    meta = _read_json(task_dir / "task_metadata.json", {})
    history = _read_json(task_dir / "api_conversation_history.json", [])
    if not isinstance(messages, list):
        messages = []
    if not isinstance(meta, dict):
        meta = {}
    if not isinstance(history, list):
        history = []
    plan = build_plan(messages, meta, history)
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
