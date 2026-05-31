#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Install claude-capsule and register its Claude Code hooks.
# Idempotent: re-running will not duplicate hook entries.
set -euo pipefail

REPO="git+https://github.com/quantumpipes/claude-capsule"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
HOOK_CMD="${CLAUDE_CAPSULE_HOOK_CMD:-claude-capsule-hook}"

echo "==> Installing claude-capsule"
if command -v pipx >/dev/null 2>&1; then
  pipx install "$REPO" || pipx upgrade claude-capsule || true
else
  python3 -m pip install --user "$REPO"
fi

# Resolve an absolute path to the hook command if it is not on PATH.
if ! command -v "$HOOK_CMD" >/dev/null 2>&1; then
  CANDIDATE="$(python3 -c 'import shutil,sys; print(shutil.which("claude-capsule-hook") or "")')"
  [ -n "$CANDIDATE" ] && HOOK_CMD="$CANDIDATE"
fi
echo "==> Hook command: $HOOK_CMD"

echo "==> Registering Stop + SessionEnd hooks in $SETTINGS"
mkdir -p "$(dirname "$SETTINGS")"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

SETTINGS="$SETTINGS" HOOK_CMD="$HOOK_CMD" python3 - <<'PY'
import json, os, sys

path = os.environ["SETTINGS"]
cmd = os.environ["HOOK_CMD"]

with open(path) as f:
    try:
        data = json.load(f)
    except json.JSONDecodeError:
        print(f"!! {path} is not valid JSON; refusing to modify it.", file=sys.stderr)
        sys.exit(1)

hooks = data.setdefault("hooks", {})
entry = {"type": "command", "command": cmd}

def already(arr):
    for group in arr:
        for h in group.get("hooks", []):
            if h.get("command") == cmd:
                return True
    return False

for event in ("Stop", "SessionEnd"):
    arr = hooks.setdefault(event, [])
    if already(arr):
        print(f"   {event}: already registered, skipping")
    else:
        arr.append({"hooks": [entry]})
        print(f"   {event}: added")

with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("==> settings.json updated")
PY

echo
echo "Done. Every Claude Code session now seals into ~/.claude-capsule/chains/<session>.db"
echo "Browse them:  git clone https://github.com/quantumpipes/claude-capsule"
echo "              cd claude-capsule/explorer && npm install && npm run export && npm run dev"
