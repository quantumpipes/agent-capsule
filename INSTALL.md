# Installing claude-capsule with Claude Code

This is the instruction set Claude Code follows to install claude-capsule. You
do not run these steps by hand; you paste the short prompt from the
[README](README.md#install-with-claude-code-copy--paste) and Claude Code reads
this page and does all of it. The steps are written for the agent.

The goal: seal every Claude Code session into a tamper-evident, cryptographically
signed hashchain by installing the package and registering its hooks.

## Steps for the agent

### 1. Install the package

Try `pipx` first, fall back to `pip --user`:

```bash
pipx install git+https://github.com/quantumpipes/claude-capsule
# or:
python3 -m pip install --user git+https://github.com/quantumpipes/claude-capsule
```

Confirm the `claude-capsule` and `claude-capsule-hook` commands are on PATH
(e.g. `claude-capsule --help`). If they are not, find their absolute path and use
that absolute path wherever `claude-capsule-hook` appears in step 2.

### 2. Register the hooks

Edit `~/.claude/settings.json`. Create the file and any missing keys if needed,
and do **not** remove or overwrite any hooks already present. Add an entry that
runs `claude-capsule-hook` (or its absolute path from step 1) to **both** the
`Stop` and `SessionEnd` hook events. The shape Claude Code expects:

```json
{
  "hooks": {
    "Stop":       [ { "hooks": [ { "type": "command", "command": "claude-capsule-hook" } ] } ],
    "SessionEnd": [ { "hooks": [ { "type": "command", "command": "claude-capsule-hook" } ] } ]
  }
}
```

Merge into the existing JSON. If a `Stop` or `SessionEnd` array already exists,
append the entry to it instead of replacing it. Validate that the JSON parses
before saving.

### 3. Verify the install end-to-end

Do not wait for a real session. Pick any transcript under
`~/.claude/projects/**/` (a `*.jsonl` file), then run:

```bash
claude-capsule-hook --transcript "<that file>" --session install-check --finalize
claude-capsule verify ~/.claude-capsule/chains/install-check.db --signatures
```

Report the verify result, then delete the throwaway chain:

```bash
rm -f ~/.claude-capsule/chains/install-check.db ~/.claude-capsule/chains/install-check.checkpoint.json
```

### 4. Report back

Tell the user:

- hooks are registered for `Stop` and `SessionEnd`,
- chains will be written to `~/.claude-capsule/chains/`,
- and the command to browse them later:

  ```bash
  git clone https://github.com/quantumpipes/claude-capsule
  cd claude-capsule/explorer && npm install && npm run export && npm run dev   # http://localhost:4840
  ```

## Privacy

Do not print the user's key material. The signing key at `~/.claude-capsule/key`
is private; only the public key is ever shared. No data leaves the machine.

## Doing it without Claude Code

```bash
pipx install git+https://github.com/quantumpipes/claude-capsule
curl -fsSL https://raw.githubusercontent.com/quantumpipes/claude-capsule/main/install.sh | bash
```

`install.sh` performs steps 1 and 2 idempotently. See the
[README](README.md#manual-install) for the by-hand settings.json edit.
