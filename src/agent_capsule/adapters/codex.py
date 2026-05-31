# SPDX-License-Identifier: Apache-2.0
"""Turn a Codex CLI session into a tamper-evident capsule hashchain.

Wired as a Codex ``notify`` program. Codex only fires ONE event,
``agent-turn-complete`` (there is no session-end event), invoking the program
with a single argv: a JSON string (``sys.argv[1]``) carrying ``thread-id``,
``cwd``, ``input-messages`` and ``last-assistant-message``.

The notify payload is a TRIGGER, not the source of truth. The source of truth
is the rollout JSONL Codex writes under::

    ~/.codex/sessions/YYYY/MM/DD/rollout-<TIMESTAMP>-<UUID>.jsonl

We locate the rollout whose ``session_meta.id`` matches ``thread-id`` (newest
first; fall back to the newest rollout), parse it in order, and seal one
capsule per tool call (``function_call``) and one per assistant/user message.

Because there is no session-end event we seal PER TURN: each fire re-seals the
rollout, and ``seal_specs`` idempotency (keyed on ``call_id`` / line index)
means only new actions are appended. The chain is finalized (verified) when a
``task_complete`` event_msg is present in the rollout.

Codex's default rollout persistence may be FILTERED (only some record types
land on disk), so we parse defensively and seal whatever is present rather than
assuming any record exists.

Storage (first match wins):
  AGENT_CAPSULE_DB=<path>  -> one shared SQLite file, grouped by session_id
  (default)                -> ~/.agent-capsule/chains/codex/<session_id>.db

Fail-open: every error is logged and the process exits 0, so the adapter can
never block or stall a Codex turn.

Standalone usage (testing without registering the notify program):
  python -m agent_capsule.adapters.codex --rollout <path.jsonl> --session <id> [--finalize]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from ..core.capsule import TYPE_CHAT, TYPE_SYSTEM, TYPE_TOOL
from ..core.sealing import FULL, SUMMARY_CAP, seal_specs
from ..core.sealing import log as _seal_log
from ..core.sealing import trunc as _trunc

TOOL = "codex"

# approval policies that mean a human gate stood between intent and action
_GATED_APPROVAL = {"untrusted", "on-request", "on-write", "always", "unless-trusted"}
# approval policies that mean the agent acted on its own
_AUTONOMOUS_APPROVAL = {"never", "on-failure", "auto", "full-auto"}

CODEX_HOME = Path(os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex")))
CONFIG_PATH = CODEX_HOME / "config.toml"
SESSIONS_DIR = CODEX_HOME / "sessions"

_NOTIFY_PROGRAM = "agent-capsule-codex-notify"
_NOTIFY_LINE = f'notify = ["{_NOTIFY_PROGRAM}"]'


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    _seal_log(TOOL, msg)


def _content_text(content: Any) -> str:
    """Join the text out of a message ``content`` (list of blocks or str)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text"):
            t = b.get("text")
            if t:
                out.append(str(t))
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out).strip()


def _arg_target(name: str, args: Any) -> str:
    """One-line target for a tool call summary."""
    a = args if isinstance(args, dict) else {}
    val = (
        a.get("command") or a.get("cmd") or a.get("path") or a.get("file_path")
        or a.get("pattern") or a.get("query") or a.get("input") or ""
    )
    if isinstance(val, list):
        val = " ".join(str(x) for x in val)
    return str(val).replace("\n", " ").strip()[:200]


def tool_summary(name: str, args: Any) -> str:
    target = _arg_target(name, args)
    return f"{name}: {target}" if target else f"{name} call"


def _output_text(output: Any) -> tuple[Any, bool | None]:
    """function_call_output.output may be a str, a dict, or a JSON string.

    Returns (text, success). ``success`` is None when the output gives no signal,
    otherwise a bool parsed from a ``success``/``metadata.exit_code`` field.
    """
    success: bool | None = None
    if isinstance(output, dict):
        if "success" in output:
            success = bool(output.get("success"))
        meta = output.get("metadata")
        if isinstance(meta, dict) and meta.get("exit_code") is not None:
            try:
                success = int(meta["exit_code"]) == 0
            except (TypeError, ValueError):
                pass
        # Codex sometimes wraps as {"output": "...", "metadata": {...}}
        if "output" in output:
            return output["output"], success
        return output, success
    return output, success


def _reasoning_text(payload: dict[str, Any]) -> str:
    """Pull the model chain-of-thought out of a reasoning response_item.

    Prefer the verbose ``content`` blocks (reasoning_text/text); fall back to the
    ``summary`` blocks (summary_text). Returns "" when nothing readable survives
    (e.g. only ``encrypted_content`` is present).
    """
    for key in ("content", "summary"):
        blocks = payload.get(key)
        if not isinstance(blocks, list):
            continue
        out: list[str] = []
        for b in blocks:
            if isinstance(b, dict) and b.get("type") in (
                "reasoning_text", "text", "summary_text",
            ):
                t = b.get("text")
                if t:
                    out.append(str(t))
            elif isinstance(b, str):
                out.append(b)
        joined = "\n".join(out).strip()
        if joined:
            return joined
    return ""


# --------------------------------------------------------------------------- #
# apply_patch (V4A envelope) -> readable unified diff
# --------------------------------------------------------------------------- #
_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_FILE_OPS = (
    ("*** Update File: ", "edited"),
    ("*** Add File: ", "added"),
    ("*** Delete File: ", "deleted"),
)


def _find_patch_envelope(args: Any) -> str | None:
    """Locate a V4A ``*** Begin Patch ... *** End Patch`` envelope in tool args.

    apply_patch passes it as ``input``/``patch``; a ``shell`` heredoc passes it
    inside the joined ``command`` argv. Returns the raw envelope text or None.
    """
    candidates: list[Any] = []
    if isinstance(args, dict):
        for k in ("input", "patch", "apply_patch", "content"):
            if args.get(k):
                candidates.append(args[k])
        cmd = args.get("command") or args.get("cmd")
        if isinstance(cmd, list):
            candidates.append("\n".join(str(x) for x in cmd))
        elif cmd:
            candidates.append(cmd)
    elif isinstance(args, str):
        candidates.append(args)
    for c in candidates:
        if not isinstance(c, str):
            continue
        start = c.find(_PATCH_BEGIN)
        if start == -1:
            continue
        end = c.find(_PATCH_END, start)
        if end == -1:
            return c[start:]
        return c[start:end + len(_PATCH_END)]
    return None


def render_apply_patch(envelope: str) -> tuple[str, int, int, list[str]]:
    """Render a V4A patch envelope into unified-diff text.

    Returns (diff_text, added, removed, side_effects). Lines beginning with ``+``
    count as added, ``-`` as removed; ``@@`` hunk headers and context lines pass
    through. Each file op contributes a ``<verb> <path>`` side effect.
    """
    out: list[str] = []
    side: list[str] = []
    added = removed = 0
    for raw in envelope.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if stripped in (_PATCH_BEGIN, _PATCH_END):
            continue
        matched = False
        for prefix, verb in _FILE_OPS:
            if line.startswith(prefix):
                path = line[len(prefix):].strip()
                out.append(f"--- {verb}: {path}")
                side.append(f"{verb} {path}")
                matched = True
                break
        if matched:
            continue
        if line.startswith("*** Move to: "):
            out.append(f"--- moved to: {line[len('*** Move to: '):].strip()}")
            continue
        out.append(line)
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return "\n".join(out), added, removed, side


def _authority(approval_policy: str) -> tuple[str, str]:
    p = (approval_policy or "").strip().lower()
    if p in _GATED_APPROVAL:
        return "policy", f"approval_policy={approval_policy}"
    # default autonomous (never / on-failure / unknown)
    return "autonomous", (f"approval_policy={approval_policy}" if approval_policy else "")


# --------------------------------------------------------------------------- #
# Rollout discovery
# --------------------------------------------------------------------------- #
def load_lines(rollout_path: str) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    with open(rollout_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _first_line_meta_id(path: Path) -> str | None:
    """Read just the first line of a rollout to get session_meta.id, cheaply."""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("type") == "session_meta":
                    return ((rec.get("payload") or {}).get("id")) or None
                # first non-empty, non-meta line: give up on this file
                return None
    except Exception:
        return None
    return None


def find_rollout(thread_id: str | None) -> Path | None:
    """Locate the rollout file for ``thread_id``; newest mtime first.

    Falls back to the newest rollout file if no id matches (or no id given).
    """
    if not SESSIONS_DIR.exists():
        return None
    candidates = sorted(
        SESSIONS_DIR.glob("**/rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    if thread_id:
        for p in candidates:
            if _first_line_meta_id(p) == thread_id:
                return p
    return candidates[0]


# --------------------------------------------------------------------------- #
# Rollout -> capsule plan
# --------------------------------------------------------------------------- #
def _session_env(meta_payload: dict[str, Any]) -> dict[str, Any]:
    git = meta_payload.get("git") if isinstance(meta_payload.get("git"), dict) else {}
    return {
        "cwd": meta_payload.get("cwd", ""),
        "cli_version": meta_payload.get("cli_version", ""),
        "originator": meta_payload.get("originator", ""),
        "model_provider": meta_payload.get("model_provider", ""),
        "forked_from_id": meta_payload.get("forked_from_id", ""),
        "git_sha": meta_payload.get("git_sha") or git.get("commit_hash", ""),
        "git_branch": meta_payload.get("git_branch") or git.get("branch", ""),
        "git_origin_url": meta_payload.get("git_origin_url") or git.get("repository_url", ""),
    }


def build_plan(records: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]], bool]:
    """Walk rollout lines in order -> (session_id, specs, finalize).

    ``finalize`` is True if a ``task_complete`` event was seen.
    """
    session_id: str | None = None
    base_env: dict[str, Any] = {}
    base_instructions = ""
    model = ""
    approval_policy = ""
    sandbox_policy = ""
    turn_extras: dict[str, Any] = {}
    current_prompt = ""
    pending_thinking = ""
    finalize = False

    # First pass: collect function_call_output keyed by call_id, so a tool
    # spec can carry its result (and success flag) even if the output line came
    # later than the call.
    outputs: dict[str, Any] = {}
    successes: dict[str, bool | None] = {}
    for r in records:
        if r.get("type") == "response_item":
            p = r.get("payload") or {}
            if p.get("type") == "function_call_output":
                cid = p.get("call_id")
                if cid:
                    text, ok = _output_text(p.get("output"))
                    outputs[cid] = text
                    successes[cid] = ok

    specs: list[dict[str, Any]] = []
    last_spec: dict[str, Any] | None = None

    def env_for() -> dict[str, Any]:
        e = dict(base_env)
        e.update(model=model, approval_policy=approval_policy, sandbox_policy=sandbox_policy)
        e.update(turn_extras)
        return e

    def take_thinking() -> str:
        nonlocal pending_thinking
        t, pending_thinking = pending_thinking, ""
        return t

    for idx, r in enumerate(records):
        rtype = r.get("type")
        payload = r.get("payload") or {}

        if rtype == "session_meta":
            session_id = payload.get("id") or session_id
            base_env = _session_env(payload)
            base_instructions = str(payload.get("base_instructions") or "").strip()
            if base_instructions:
                specs.append({
                    "key": f"sysprompt:{idx}",
                    "type": TYPE_SYSTEM,
                    "prompt": "",
                    "agent_id": TOOL,
                    "env": dict(base_env),
                    "model": model,
                    "narrative": base_instructions,
                    "summary": "session base instructions (system prompt)",
                    "status": "success",
                })
            continue

        if rtype == "turn_context":
            model = payload.get("model") or model
            approval_policy = payload.get("approval_policy") or approval_policy
            sandbox_policy = payload.get("sandbox_policy") or sandbox_policy
            extras = {
                "reasoning_effort": payload.get("effort") or payload.get("reasoning_effort") or "",
                "reasoning_summary": payload.get("summary") or "",
                "timezone": payload.get("timezone") or "",
                "workspace_roots": payload.get("workspace_roots") or [],
            }
            turn_extras = {k: v for k, v in extras.items() if v}
            continue

        if rtype == "compacted":
            summary = (
                payload.get("message")
                or payload.get("summary")
                or payload.get("replacement_history")
                or ""
            )
            if isinstance(summary, (list, dict)):
                summary = _content_text(summary) if isinstance(summary, list) else json.dumps(summary)
            summary = str(summary).strip()
            if summary:
                specs.append({
                    "key": f"compact:{idx}",
                    "type": TYPE_SYSTEM,
                    "prompt": current_prompt,
                    "agent_id": TOOL,
                    "env": env_for(),
                    "model": model,
                    "narrative": summary,
                    "summary": "history compaction",
                    "status": "success",
                })
            continue

        if rtype == "response_item":
            inner = payload.get("type")

            if inner == "reasoning":
                # Accumulate chain-of-thought; carry it onto the NEXT action.
                rt = _reasoning_text(payload)
                if rt:
                    pending_thinking = (pending_thinking + "\n\n" + rt).strip() if pending_thinking else rt
                continue

            if inner == "function_call":
                name = payload.get("name", "?")
                raw_args = payload.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": raw_args}
                cid = payload.get("call_id") or f"call:{idx}"
                authority, policy_ref = _authority(approval_policy)
                ok = successes.get(cid)
                ok = True if ok is None else ok
                result = outputs.get(cid)
                summary_extra = ""
                side_effects: list[str] = []

                # apply_patch (or a shell heredoc carrying a V4A envelope):
                # render the patch into a readable unified diff, keep the raw
                # patch in arguments, and surface +/- counts + edited paths.
                envelope = None
                if name in ("apply_patch", "shell", "local_shell"):
                    envelope = _find_patch_envelope(args)
                if envelope:
                    diff, added, removed, side_effects = render_apply_patch(envelope)
                    if diff:
                        result = diff
                        summary_extra = f" (+{added}/-{removed})"

                spec = {
                    "key": cid,
                    "type": TYPE_TOOL,
                    "prompt": current_prompt,
                    "agent_id": TOOL,
                    "env": env_for(),
                    "model": model,
                    "thinking": take_thinking(),
                    "authority_type": authority,
                    "policy_reference": policy_ref,
                    "tool": name,
                    "arguments": args,
                    "result": result,
                    "success": ok,
                    "side_effects": side_effects,
                    "summary": tool_summary(name, args) + summary_extra + ("" if ok else " (failed)"),
                    "status": "success" if ok else "failure",
                }
                specs.append(spec)
                last_spec = spec
                continue

            if inner in ("web_search_call", "web_search"):
                action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
                query = payload.get("query") or action.get("query") or ""
                spec = {
                    "key": payload.get("id") or f"websearch:{idx}",
                    "type": TYPE_TOOL,
                    "prompt": current_prompt,
                    "agent_id": TOOL,
                    "env": env_for(),
                    "model": model,
                    "thinking": take_thinking(),
                    "tool": "web_search",
                    "arguments": {"query": query, "action": action or None},
                    "result": {"query": query, "status": payload.get("status", "")},
                    "success": True,
                    "summary": f"web_search: {query}"[:200] if query else "web_search",
                    "status": "success",
                }
                specs.append(spec)
                last_spec = spec
                continue

            if inner == "message":
                role = payload.get("role")
                text = _content_text(payload.get("content"))
                if role == "assistant" and text:
                    spec = {
                        "key": f"msg:{idx}",
                        "type": TYPE_CHAT,
                        "prompt": current_prompt,
                        "agent_id": TOOL,
                        "env": env_for(),
                        "model": model,
                        "thinking": take_thinking(),
                        "narrative": text,
                        "response": text,
                        "summary": _trunc(text, SUMMARY_CAP),
                        "status": "success",
                    }
                    specs.append(spec)
                    last_spec = spec
                elif role in ("user", "system") and text:
                    current_prompt = text
                continue
            # other response_item kinds: ignore
            continue

        if rtype == "event_msg":
            inner = payload.get("type")

            if inner == "agent_message":
                text = str(payload.get("message", "")).strip()
                if text:
                    spec = {
                        "key": f"msg:{idx}",
                        "type": TYPE_CHAT,
                        "prompt": current_prompt,
                        "agent_id": TOOL,
                        "env": env_for(),
                        "model": model,
                        "thinking": take_thinking(),
                        "narrative": text,
                        "response": text,
                        "summary": _trunc(text, SUMMARY_CAP),
                        "status": "success",
                    }
                    specs.append(spec)
                    last_spec = spec
                continue

            if inner == "user_message":
                text = str(payload.get("message", "")).strip()
                if text:
                    current_prompt = text
                continue

            if inner == "web_search_end":
                query = str(payload.get("query", "")).strip()
                spec = {
                    "key": f"websearch:{idx}",
                    "type": TYPE_TOOL,
                    "prompt": current_prompt,
                    "agent_id": TOOL,
                    "env": env_for(),
                    "model": model,
                    "tool": "web_search",
                    "arguments": {"query": query, "action": payload.get("action")},
                    "result": {"query": query},
                    "success": True,
                    "summary": f"web_search: {query}"[:200] if query else "web_search",
                    "status": "success",
                }
                specs.append(spec)
                last_spec = spec
                continue

            if inner == "token_count":
                info = payload.get("info") or {}
                usage = info.get("total_token_usage") or info.get("last_token_usage") or {}
                # full breakdown: keep every field Codex reports, plus the
                # model context window if present at info level.
                usage = dict(usage) if usage else {}
                if info.get("model_context_window") is not None:
                    usage.setdefault("model_context_window", info["model_context_window"])
                rate_limits = payload.get("rate_limits") or info.get("rate_limits")
                if usage or rate_limits:
                    if last_spec is not None and usage:
                        # attach the full usage to the most recent spec
                        last_spec["usage"] = _trunc(usage, FULL)
                        if rate_limits:
                            last_spec.setdefault("env", {})["rate_limits"] = _trunc(rate_limits, FULL)
                    else:
                        sp = {
                            "key": f"usage:{idx}",
                            "type": TYPE_SYSTEM,
                            "prompt": current_prompt,
                            "agent_id": TOOL,
                            "env": env_for(),
                            "model": model,
                            "usage": usage,
                            "summary": "token usage",
                            "status": "success",
                        }
                        if rate_limits:
                            sp["env"]["rate_limits"] = _trunc(rate_limits, FULL)
                        specs.append(sp)
                continue

            if inner == "task_complete":
                finalize = True
                continue
            # task_started and other events: ignore
            continue
        # unknown wrappers: ignore
        continue

    # Reasoning that trailed with no following action: emit a small system
    # capsule so the chain-of-thought is never silently dropped.
    if pending_thinking:
        specs.append({
            "key": f"reasoning:{len(records)}",
            "type": TYPE_SYSTEM,
            "prompt": current_prompt,
            "agent_id": TOOL,
            "env": env_for(),
            "model": model,
            "thinking": pending_thinking,
            "summary": "model reasoning (no following action)",
            "status": "success",
        })

    return session_id, specs, finalize


# --------------------------------------------------------------------------- #
# Seal
# --------------------------------------------------------------------------- #
def run(rollout_path: str, session_override: str | None = None, finalize: bool = False) -> dict[str, Any]:
    records = load_lines(rollout_path)
    session_id, specs, saw_complete = build_plan(records)
    session_id = session_override or session_id or Path(rollout_path).stem
    do_finalize = finalize or saw_complete

    shared = os.environ.get("AGENT_CAPSULE_DB")
    if shared:
        return seal_specs(TOOL, session_id, specs, finalize=do_finalize,
                          db_path=Path(os.path.expanduser(shared)), tenant_id=session_id)
    return seal_specs(TOOL, session_id, specs, finalize=do_finalize)


# --------------------------------------------------------------------------- #
# install / uninstall
# --------------------------------------------------------------------------- #
def _existing_notify(text: str) -> Any:
    """Return the value of a top-level ``notify`` key in TOML, or None."""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover (py<3.11)
        return None
    try:
        return tomllib.loads(text).get("notify")
    except Exception:
        return None


def install() -> None:
    """Ensure ``~/.codex/config.toml`` has our notify program registered.

    USER-level config only (Codex ignores project-local config for notify).
    Never clobbers a different existing ``notify``; logs guidance instead.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    existing = _existing_notify(text)

    if existing is not None:
        if isinstance(existing, list) and _NOTIFY_PROGRAM in existing:
            _log(f"install: already registered in {CONFIG_PATH}")
            print(f"agent-capsule codex notify already registered in {CONFIG_PATH}")
            return
        # a DIFFERENT notify exists: do not clobber
        _log(f"install: WARNING existing notify={existing!r} in {CONFIG_PATH}; not modified")
        print(
            f"WARNING: {CONFIG_PATH} already sets notify = {existing!r}.\n"
            f"Codex supports a single notify program. To capsule Codex turns, add\n"
            f"  {_NOTIFY_PROGRAM}\n"
            f"to that program's chain, or replace notify with:\n  {_NOTIFY_LINE}"
        )
        return

    addition = (
        "\n# Added by agent-capsule: seal each completed Codex turn into a capsule chain.\n"
        f"{_NOTIFY_LINE}\n"
    )
    # ensure a separating newline before our appended block
    if text and not text.endswith("\n"):
        new_text = text + "\n" + addition.lstrip("\n")
    else:
        new_text = text + addition
    CONFIG_PATH.write_text(new_text, encoding="utf-8")
    _log(f"install: registered notify in {CONFIG_PATH}")
    print(f"Registered agent-capsule codex notify in {CONFIG_PATH}")


def uninstall() -> None:
    """Remove the notify line agent-capsule added; leave the rest untouched."""
    if not CONFIG_PATH.exists():
        return
    text = CONFIG_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    removed = False
    for ln in lines:
        stripped = ln.strip()
        if stripped == _NOTIFY_LINE or (stripped.startswith("notify") and _NOTIFY_PROGRAM in stripped):
            removed = True
            continue
        if stripped.startswith("# Added by agent-capsule"):
            removed = True
            continue
        kept.append(ln)
    if removed:
        CONFIG_PATH.write_text("".join(kept), encoding="utf-8")
        _log(f"uninstall: removed notify from {CONFIG_PATH}")
        print(f"Removed agent-capsule codex notify from {CONFIG_PATH}")
    else:
        print(f"agent-capsule codex notify not found in {CONFIG_PATH}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        ap = argparse.ArgumentParser(
            prog=_NOTIFY_PROGRAM,
            description="Codex CLI turn -> capsule hashchain (notify program)",
        )
        ap.add_argument("--rollout", help="rollout JSONL path (test/offline mode)")
        ap.add_argument("--session", help="session id override")
        ap.add_argument("--finalize", action="store_true", help="verify chain after appending")
        ap.add_argument("--install", action="store_true", help="register the notify program")
        ap.add_argument("--uninstall", action="store_true", help="remove the notify program")
        # the notify JSON arrives as a bare positional argv (sys.argv[1])
        ap.add_argument("notify_json", nargs="?", help="Codex notify JSON string")
        args, _unknown = ap.parse_known_args(argv)

        if args.install:
            install()
            return 0
        if args.uninstall:
            uninstall()
            return 0

        rollout = args.rollout
        session = args.session
        finalize = args.finalize

        if not rollout:
            thread_id = None
            if args.notify_json:
                try:
                    event = json.loads(args.notify_json)
                except Exception as e:
                    _log(f"notify JSON parse failed: {e}")
                    return 0
                if event.get("type") and event.get("type") != "agent-turn-complete":
                    _log(f"ignoring notify type={event.get('type')!r}")
                    return 0
                thread_id = event.get("thread-id")
                session = session or thread_id
            found = find_rollout(thread_id)
            if found is None:
                _log(f"no rollout found (thread_id={thread_id!r}, dir={SESSIONS_DIR})")
                return 0
            rollout = str(found)

        if not rollout or not Path(rollout).exists():
            _log(f"rollout missing: {rollout!r}")
            return 0

        run(rollout, session_override=session, finalize=finalize)
    except Exception:
        _log("ERROR\n" + traceback.format_exc())
    return 0  # always fail-open


if __name__ == "__main__":
    sys.exit(main())
