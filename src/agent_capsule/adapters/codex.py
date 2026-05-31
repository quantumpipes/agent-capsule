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
from ..core.sealing import SUMMARY_CAP, seal_specs
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


def _output_text(output: Any) -> Any:
    """function_call_output.output may be a str, a dict, or a JSON string."""
    if isinstance(output, dict):
        # Codex sometimes wraps as {"output": "...", "metadata": {...}}
        if "output" in output:
            return output["output"]
        return output
    return output


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
    return {
        "cwd": meta_payload.get("cwd", ""),
        "cli_version": meta_payload.get("cli_version", ""),
        "originator": meta_payload.get("originator", ""),
        "model_provider": meta_payload.get("model_provider", ""),
        "git_sha": meta_payload.get("git_sha", ""),
        "git_branch": meta_payload.get("git_branch", ""),
        "git_origin_url": meta_payload.get("git_origin_url", ""),
    }


def build_plan(records: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]], bool]:
    """Walk rollout lines in order -> (session_id, specs, finalize).

    ``finalize`` is True if a ``task_complete`` event was seen.
    """
    session_id: str | None = None
    base_env: dict[str, Any] = {}
    model = ""
    approval_policy = ""
    sandbox_policy = ""
    current_prompt = ""
    finalize = False

    # First pass: collect function_call_output keyed by call_id, so a tool
    # spec can carry its result even if the output line came later.
    outputs: dict[str, Any] = {}
    for r in records:
        if r.get("type") == "response_item":
            p = r.get("payload") or {}
            if p.get("type") == "function_call_output":
                cid = p.get("call_id")
                if cid:
                    outputs[cid] = _output_text(p.get("output"))

    specs: list[dict[str, Any]] = []
    last_spec: dict[str, Any] | None = None

    def env_for() -> dict[str, Any]:
        e = dict(base_env)
        e.update(model=model, approval_policy=approval_policy, sandbox_policy=sandbox_policy)
        return e

    for idx, r in enumerate(records):
        rtype = r.get("type")
        payload = r.get("payload") or {}

        if rtype == "session_meta":
            session_id = payload.get("id") or session_id
            base_env = _session_env(payload)
            continue

        if rtype == "turn_context":
            model = payload.get("model") or model
            approval_policy = payload.get("approval_policy") or approval_policy
            sandbox_policy = payload.get("sandbox_policy") or sandbox_policy
            continue

        if rtype == "response_item":
            inner = payload.get("type")

            if inner == "function_call":
                name = payload.get("name", "?")
                raw_args = payload.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": raw_args}
                cid = payload.get("call_id") or f"call:{idx}"
                authority, policy_ref = _authority(approval_policy)
                spec = {
                    "key": cid,
                    "type": TYPE_TOOL,
                    "prompt": current_prompt,
                    "agent_id": TOOL,
                    "env": env_for(),
                    "model": model,
                    "authority_type": authority,
                    "policy_reference": policy_ref,
                    "tool": name,
                    "arguments": args,
                    "result": outputs.get(cid),
                    "success": True,
                    "summary": tool_summary(name, args),
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
            # reasoning / other response_item kinds: ignore (often redacted)
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

            if inner == "token_count":
                info = payload.get("info") or {}
                usage = info.get("total_token_usage") or info.get("last_token_usage") or {}
                if usage:
                    if last_spec is not None:
                        # attach to the most recent spec (merge, don't clobber)
                        last_spec["usage"] = _trunc(dict(usage), None)
                    else:
                        specs.append({
                            "key": f"usage:{idx}",
                            "type": TYPE_SYSTEM,
                            "prompt": current_prompt,
                            "agent_id": TOOL,
                            "env": env_for(),
                            "model": model,
                            "usage": dict(usage),
                            "summary": "token usage",
                            "status": "success",
                        })
                continue

            if inner == "task_complete":
                finalize = True
                continue
            # task_started and other events: ignore
            continue
        # compacted / unknown wrappers: ignore
        continue

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
