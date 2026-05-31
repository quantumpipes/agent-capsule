<!-- SPDX-License-Identifier: Apache-2.0 -->

# Writing an adapter

This is a tutorial: add support for a new AI coding agent end to end. By the
end you will have a thin parser that seals that tool's sessions into the same
tamper-evident chains as Claude Code, Cursor, Codex, and Cline, verifiable in
the same browser explorer.

You do not write any crypto. You do not touch storage. An adapter is a parser.

## 1. The mental model

Every adapter does exactly two tool-specific things:

1. **Install the trigger.** Register your tool's lifecycle hook (a hook entry, a
   `notify` program, an executable shim) so the tool runs your adapter at a
   useful moment (turn complete, session end, task done).
2. **Parse the transcript into specs.** When the trigger fires, read the tool's
   own durable transcript (its JSONL, SQLite, or task JSON) and turn it into a
   list of plain dicts called "capsule specs", then hand them to the shared
   sealer.

```
  your tool's trigger
        |
        v
  adapter.parse(transcript) -> list[spec dict]
        |
        v
  agent_capsule.core.sealing.seal_specs(tool, session_id, specs)
        |
        v
  ~/.agent-capsule/chains/<tool>/<session>.db   (signed, hash-linked)
```

The trigger is a signal, not the truth. Parse the transcript, not the event
payload, so the capsule reflects what actually happened. Everything downstream
of `seal_specs` (building the six-section capsule, SHA3-256 hashing, Ed25519
signing, chain linking, idempotent re-sealing, fail-open logging, verification)
is shared in `agent_capsule.core`. See [architecture.md](architecture.md) for
the full picture and [wire-format.md](wire-format.md) for the exact bytes.

## 2. The spec contract

A spec is a plain `dict`. You produce a list of them; `seal_specs` turns each
one into a six-section `Capsule`. The contract is defined in
`src/agent_capsule/core/sealing.py` (read `make_capsule` and the module
docstring). Only `type` and `key` really matter for correctness; everything
else is optional and fills in whatever your tool exposes.

| Key | Type | Meaning |
|-----|------|---------|
| `key` | str | Idempotency key, unique per action within a session. See below. |
| `type` | str | `"tool"`, `"chat"`, `"system"`, or `"agent"` (use the `TYPE_*` constants). |
| `prompt` | str | The user request that drove this action (-> `trigger.request`). |
| `agent_id` | str | Defaults to the tool name. Set a distinct id for sub-agents. |
| `env` | dict | Provenance: cwd, model, timestamps, git branch, versions. |
| `narrative` | str | Visible assistant prose (-> `reasoning.analysis`). |
| `thinking` | str | Hidden reasoning text, usually `""` (-> `reasoning.reasoning`). |
| `model` | str | Model name. |
| `permission_mode` | str | The tool's permission/approval mode. Drives authority (see below). |
| `authority_type` | str | Explicit `"autonomous"` / `"policy"` / `"human_approved"`. Overrides `permission_mode`. |
| `policy_reference` | str | Free text describing the authority basis. |
| `tool` | str | Tool name, if this action is a tool call. `None` for chat. |
| `arguments` | any | Tool input. |
| `result` | any | Tool output. |
| `success` | bool | Defaults to `True`. |
| `duration_ms` | int | Wall time of the action. |
| `usage` | dict | Token usage (-> `execution.resources_used`). |
| `summary` | str | One-line outcome summary. |
| `side_effects` | list[str] | For example `["wrote src/foo.py"]`. |
| `status` | str | `"success"` or `"failure"`. |
| `response` | str | Assistant response text (for chat capsules; -> `outcome.result`). |
| `structured` | any | Extra structured payload (-> `outcome.result`). |

### `key` gives you idempotency

`seal_specs` records each sealed `key` in a per-session checkpoint. Re-running
the adapter on the same transcript appends only specs whose `key` it has not
seen. This is what makes a per-turn trigger (Codex) or a fire-on-every-hook
trigger (Cline) safe: call `seal_specs` as often as you like, the chain grows
cleanly. Build keys from stable transcript identifiers, never from wall time:
a tool-call id (`call_id`, `tool_use_id`), a message uuid plus block index
(`f"{uuid}:{i}"`), or a record timestamp plus index (`f"{ts}:{index}"`).

### How authority is derived

This is the load-bearing field: it records whether the agent acted on its own
or behind a human gate. Two ways to set it:

- Set `authority_type` explicitly (`"autonomous"`, `"policy"`, or
  `"human_approved"`) when your transcript tells you directly. Codex maps its
  approval policy this way; Cline marks an `ask:"tool"` action as
  `"human_approved"`.
- Or set `permission_mode` to the tool's raw mode string and let `seal_specs`
  decide. An empty mode or a mode in `AUTONOMOUS_MODES`
  (`bypassPermissions`, `acceptEdits`, `dontAsk`, `auto`, `yolo`, `full-auto`,
  `auto-edit`) becomes `"autonomous"`; anything else becomes `"policy"`. Claude
  Code uses this path.

Pick one. `authority_type` wins if both are present.

## 3. A minimal adapter skeleton

Copy this into `src/agent_capsule/adapters/<tool>.py` and adapt the parser to
your tool's transcript. The example parses a hypothetical newline-delimited
JSON transcript where each line is a record: a `prompt`, a `tool_call` (with
its result), or an assistant `message`. Keep the structure (`TOOL`, `parse`,
`run`, fail-open `main`, `install`/`uninstall`); change only what the
transcript shape demands.

```python
# SPDX-License-Identifier: Apache-2.0
"""Turn an Acme Agent session into a tamper-evident capsule hashchain."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from ..core.capsule import TYPE_CHAT, TYPE_TOOL
from ..core.sealing import SUMMARY_CAP, seal_specs
from ..core.sealing import log as _seal_log
from ..core.sealing import trunc as _trunc

TOOL = "acme"


def _log(msg: str) -> None:
    _seal_log(TOOL, msg)


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
                continue  # parse defensively; never assume a record exists
    return recs


def parse(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk the transcript in order into a list of capsule specs."""
    specs: list[dict[str, Any]] = []
    current_prompt = ""
    mode = ""

    for idx, r in enumerate(records):
        kind = r.get("kind")

        if kind == "prompt":
            current_prompt = str(r.get("text", ""))
            continue

        if kind == "settings":
            mode = str(r.get("permission_mode", "")) or mode
            continue

        if kind == "tool_call":
            name = str(r.get("name", "?"))
            args = r.get("input", {})
            ok = not r.get("is_error", False)
            specs.append({
                "key": str(r.get("id") or f"call:{idx}"),  # idempotency
                "type": TYPE_TOOL,
                "prompt": current_prompt,
                "agent_id": TOOL,
                "env": {"cwd": r.get("cwd", ""), "timestamp": r.get("ts", "")},
                "model": r.get("model", ""),
                "permission_mode": mode,  # drives authority
                "tool": name,
                "arguments": args,
                "result": r.get("output"),
                "success": ok,
                "side_effects": ([f"wrote {args['file_path']}"]
                                 if isinstance(args, dict) and args.get("file_path") else []),
                "duration_ms": r.get("duration_ms"),
                "summary": f"{name}: {str(args)[:120]}",
                "status": "success" if ok else "failure",
            })
            continue

        if kind == "message" and r.get("role") == "assistant":
            text = str(r.get("text", "")).strip()
            if not text:
                continue
            specs.append({
                "key": f"msg:{idx}",
                "type": TYPE_CHAT,
                "prompt": current_prompt,
                "agent_id": TOOL,
                "env": {"timestamp": r.get("ts", "")},
                "model": r.get("model", ""),
                "permission_mode": mode,
                "narrative": text,
                "response": text,
                "usage": r.get("usage") or {},
                "summary": _trunc(text, SUMMARY_CAP),
                "status": "success",
            })

    return specs


def run(session_id: str, transcript_path: str, finalize: bool) -> dict[str, Any]:
    specs = parse(load_records(transcript_path))
    shared = os.environ.get("AGENT_CAPSULE_DB")
    if shared:
        return seal_specs(TOOL, session_id, specs, finalize=finalize,
                          db_path=Path(os.path.expanduser(shared)), tenant_id=session_id)
    return seal_specs(TOOL, session_id, specs, finalize=finalize)


def install() -> None:
    """Register the trigger so the tool runs this adapter. See section 5."""
    raise NotImplementedError


def uninstall() -> None:
    """Remove only the trigger this adapter added; leave the rest untouched."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    """Fail-open entry point. Always returns 0 so it can never block the tool."""
    try:
        ap = argparse.ArgumentParser(description="Acme Agent session -> capsule hashchain")
        ap.add_argument("--transcript", help="transcript path (test/offline mode)")
        ap.add_argument("--session", help="session id")
        ap.add_argument("--finalize", action="store_true", help="verify chain after appending")
        ap.add_argument("--install", action="store_true")
        ap.add_argument("--uninstall", action="store_true")
        args, _unknown = ap.parse_known_args(argv if argv is not None else sys.argv[1:])

        if args.install:
            install()
            return 0
        if args.uninstall:
            uninstall()
            return 0

        transcript = args.transcript
        session = args.session
        if not transcript:
            # Real trigger path: read the event from stdin or argv, then locate
            # the durable transcript. The event is a signal, not the source.
            ...
        if not transcript or not Path(transcript).exists():
            _log(f"transcript missing: {transcript!r}")
            return 0
        if not session:
            session = Path(transcript).stem
        run(session, transcript, args.finalize)
    except Exception:
        _log("ERROR\n" + traceback.format_exc())
    return 0  # always fail-open


if __name__ == "__main__":
    sys.exit(main())
```

Three rules this skeleton encodes, all non-negotiable:

- **Fail-open.** Every code path returns 0 and logs via `log(TOOL, msg)`. An
  audit tool must never break the thing it audits.
- **Parse defensively.** Skip records you cannot parse; never assume a given
  record type exists. Some tools filter what they persist.
- **Stable keys.** Derive `key` from the transcript, not from `datetime.now()`,
  so re-runs are idempotent.

## 4. Wiring it up

Two edits make `agent-capsule install acme` work and give the trigger a
command to call.

Add the console-script entry to `pyproject.toml` under `[project.scripts]`:

```toml
agent-capsule-acme-hook = "agent_capsule.adapters.acme:main"
```

Add the tool to the `_ADAPTERS` map in `src/agent_capsule/cli.py` (the key is
the user-facing name, the value is the module file stem):

```python
_ADAPTERS = {"claude-code": "claude_code", "cursor": "cursor",
             "codex": "codex", "cline": "cline", "acme": "acme"}
```

That is all the CLI needs. `agent-capsule install acme` now imports your module
and calls `install()`; `agent-capsule uninstall acme` calls `uninstall()`.

## 5. Writing `install()`

Your tool exposes one of three trigger shapes. Pick the matching pattern. All
three share two rules: **idempotent** (running install twice changes nothing)
and **never clobber** (do not destroy config or hooks the user already had).

### Pattern A: merge a JSON config (like Cursor)

Read the JSON, add your entry only if it is absent, write it back. Preserve
everything else.

```python
HOOK_COMMAND = "agent-capsule-acme-hook"
HOOKS_JSON = Path(os.path.expanduser("~/.acme/hooks.json"))

def install() -> None:
    HOOKS_JSON.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if HOOKS_JSON.exists():
        try:
            data = json.loads(HOOKS_JSON.read_text())
        except json.JSONDecodeError as e:
            print(f"refusing to modify {HOOKS_JSON}: not valid JSON ({e})")
            return
    hooks = data.setdefault("hooks", {})
    entry = {"type": "command", "command": HOOK_COMMAND}
    for event in ("stop", "sessionEnd"):
        arr = hooks.setdefault(event, [])
        if not any(isinstance(h, dict) and h.get("command") == HOOK_COMMAND for h in arr):
            arr.append(dict(entry))
    HOOKS_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
```

### Pattern B: append a line to a config (like Codex)

When the tool takes a single program, do not stomp an existing value. Append
your line if no relevant config exists; if a different one is set, print
guidance and leave it alone.

```python
CONFIG_PATH = Path(os.path.expanduser("~/.acme/config.toml"))
_NOTIFY_PROGRAM = "agent-capsule-acme-hook"
_NOTIFY_LINE = f'notify = ["{_NOTIFY_PROGRAM}"]'

def install() -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    if _NOTIFY_PROGRAM in text:
        print(f"already registered in {CONFIG_PATH}")
        return
    if "notify" in text:  # a different notify exists: do not clobber
        print(f"WARNING: {CONFIG_PATH} already sets a notify program; not modified.")
        return
    sep = "" if (not text or text.endswith("\n")) else "\n"
    CONFIG_PATH.write_text(text + sep + _NOTIFY_LINE + "\n", encoding="utf-8")
    print(f"Registered {_NOTIFY_PROGRAM} in {CONFIG_PATH}")
```

### Pattern C: write executable hook shims (like Cline)

When the tool runs scripts from a directory, write short shims that exec your
console script, and mark them executable. Overwrite only your own shim files.

```python
import stat

HOOK_NAMES = ("TaskComplete", "TaskCancel", "TaskStart")
_HOOKS_DIR = Path(os.path.expanduser("~")) / "Documents" / "Acme" / "Hooks"
_POSIX_SHIM = "#!/bin/sh\nexec agent-capsule-acme-hook\n"

def install() -> None:
    _HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH  # 0755
    for name in HOOK_NAMES:
        shim = _HOOKS_DIR / name
        shim.write_text(_POSIX_SHIM, encoding="utf-8")
        try:
            os.chmod(shim, mode)
        except OSError as e:
            _log(f"chmod {shim} failed: {e}")
```

For each, `uninstall()` is the mirror: remove only the entries, lines, or files
this adapter created, and leave everything else exactly as it was.

## 6. Storage

You never touch storage. `seal_specs` writes to
`~/.agent-capsule/chains/<tool>/<session>.db` automatically, one SQLite chain
per session, namespaced by your `TOOL` string (see
`src/agent_capsule/core/paths.py`). The signing key, the log, and the
checkpoint all live under `~/.agent-capsule` and are managed for you.

Two things you can override, both already wired in the skeleton's `run`:

- `AGENT_CAPSULE_DB` (env var): when set, all sessions go into one shared
  SQLite file, grouped by `tenant_id=session_id`. Pass `db_path` and
  `tenant_id` to `seal_specs` for this mode.
- `AGENT_CAPSULE_HOME` (env var): relocates the whole `~/.agent-capsule` tree.
  Tests use this to seal into a tmp dir.

## 7. Testing your adapter

Build a synthetic transcript, run the adapter against a tmp `AGENT_CAPSULE_HOME`,
then assert the chain verifies and that a one-byte tamper breaks it. Put this in
`tests/test_acme.py`.

```python
# SPDX-License-Identifier: Apache-2.0
import json
import sqlite3

import pytest

from agent_capsule.adapters import acme
from agent_capsule.core.chain import CapsuleChain
from agent_capsule.core.seal import Seal
from agent_capsule.core.storage import CapsuleStorage

SESSION = "test-session"


def _write_transcript(path):
    lines = [
        {"kind": "settings", "permission_mode": "auto"},
        {"kind": "prompt", "text": "list the files", "ts": "2026-05-30T12:00:00Z"},
        {"kind": "tool_call", "id": "call-1", "name": "shell",
         "input": {"command": "ls -la"}, "output": "total 0\n", "ts": "2026-05-30T12:00:01Z"},
        {"kind": "message", "role": "assistant", "text": "The directory is empty.",
         "usage": {"total_tokens": 42}, "ts": "2026-05-30T12:00:02Z"},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "agent-capsule-home"
    monkeypatch.setenv("AGENT_CAPSULE_HOME", str(h))
    monkeypatch.delenv("AGENT_CAPSULE_DB", raising=False)
    # paths captures HOME at import time; reload the modules that read it
    import importlib
    from agent_capsule.core import paths as paths_mod
    importlib.reload(paths_mod)
    from agent_capsule.core import sealing as sealing_mod
    importlib.reload(sealing_mod)
    importlib.reload(acme)
    return h


def _db(home):
    return home / "chains" / "acme" / f"{SESSION}.db"


def test_seals_and_verifies(home, tmp_path):
    t = tmp_path / "session.jsonl"
    _write_transcript(t)
    assert acme.main(["--transcript", str(t), "--session", SESSION, "--finalize"]) == 0

    db = _db(home)
    assert db.exists()
    storage = CapsuleStorage(db)
    try:
        assert CapsuleChain(storage).verify(seal=Seal()).valid is True
        canon = [json.loads(r["canonical"]) for r in storage.get_all_ordered()]
        tools = [c for c in canon if c["type"] == "tool"]
        assert tools and tools[0]["execution"]["tool_calls"][0]["tool"] == "shell"
    finally:
        storage.close()


def test_idempotent_reseal(home, tmp_path):
    t = tmp_path / "session.jsonl"
    _write_transcript(t)
    acme.main(["--transcript", str(t), "--session", SESSION])
    storage = CapsuleStorage(_db(home))
    try:
        first = len(storage.get_all_ordered())
    finally:
        storage.close()
    acme.main(["--transcript", str(t), "--session", SESSION, "--finalize"])
    storage = CapsuleStorage(_db(home))
    try:
        assert len(storage.get_all_ordered()) == first  # nothing new appended
    finally:
        storage.close()


def test_tamper_breaks_verification(home, tmp_path):
    t = tmp_path / "session.jsonl"
    _write_transcript(t)
    acme.main(["--transcript", str(t), "--session", SESSION, "--finalize"])

    db = _db(home)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT rowid_pk, canonical FROM capsules ORDER BY sequence ASC LIMIT 1"
        ).fetchone()
        rowid, canonical = row[0], row[1]
        tampered = ("X" + canonical[1:]) if canonical[0] != "X" else ("Y" + canonical[1:])
        conn.execute("UPDATE capsules SET canonical = ? WHERE rowid_pk = ?", (tampered, rowid))
        conn.commit()
    finally:
        conn.close()

    storage = CapsuleStorage(db)
    try:
        result = CapsuleChain(storage).verify(seal=Seal())
        assert result.valid is False
        assert result.broken_at == 0
    finally:
        storage.close()
```

Run it without installing the package:

```bash
PYTHONPATH=src AGENT_CAPSULE_HOME=/tmp/x python3 -m pytest tests/test_acme.py
```

The `home` fixture reloads `paths` and `sealing` because they read
`AGENT_CAPSULE_HOME` at import time; reload them after setting the env var or
your test writes into the real `~/.agent-capsule`.

## 8. Checklist to open a PR

- [ ] `src/agent_capsule/adapters/<tool>.py` with `TOOL`, `parse`, `run`,
      fail-open `main`, and idempotent `install`/`uninstall`.
- [ ] `tests/test_<tool>.py` proving the chain verifies, a re-seal is
      idempotent, and a one-byte tamper breaks verification.
- [ ] `docs/tools/<tool>.md` (one page: trigger, what it captures, storage path,
      caveats; match the existing pages in [docs/tools/](tools/)).
- [ ] `[project.scripts]` entry in `pyproject.toml`:
      `agent-capsule-<tool>-hook = "agent_capsule.adapters.<tool>:main"`.
- [ ] `<tool>` added to the `_ADAPTERS` map in `cli.py`.
- [ ] A row in the supported-tools table in [../README.md](../README.md).

That is a full adapter: a parser, a trigger, a test, and a doc. The crypto, the
chain, and the explorer come along for the ride. See [cli.md](cli.md) for the
commands your users will run against the chains you produce.

---

Apache License 2.0. Copyright 2026 Quantum Pipes Technologies, LLC.
