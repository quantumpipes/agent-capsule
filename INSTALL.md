# Installing agent-capsule

agent-capsule seals your AI coding agents' sessions into tamper-evident
hashchains. Install the package once, then wire up whichever agents you use.

## 1. Install the package

Python 3.11+. The only runtime dependency is PyNaCl.

```bash
pipx install git+https://github.com/quantumpipes/agent-capsule
# or:
python3 -m pip install --user git+https://github.com/quantumpipes/agent-capsule
```

Confirm the commands are on PATH: `agent-capsule --help`.

## 2. Wire up your agents

Each command is idempotent and never clobbers config you already have.

```bash
agent-capsule install claude-code   # ~/.claude/settings.json   Stop + SessionEnd hooks
agent-capsule install cursor        # ~/.cursor/hooks.json       stop hook
agent-capsule install codex         # ~/.codex/config.toml       notify program
agent-capsule install cline         # ~/Documents/Cline/Hooks/   TaskComplete/Cancel/Start
```

Remove a hook anytime with `agent-capsule uninstall <tool>`. Per-tool details
(exact trigger, what is captured, caveats) live in [docs/tools/](docs/tools/).

## 3. Verify it works

```bash
agent-capsule list                                  # chains, grouped by tool
agent-capsule verify <chain.db> --signatures        # recompute hashes + signatures
```

Chains are written to `~/.agent-capsule/chains/<tool>/<session>.db`. Your signing
key is at `~/.agent-capsule/key` and never leaves your machine; only the public
key is shared.

---

## Installing Claude Code with Claude Code (copy / paste)

For Claude Code specifically, you can paste this into a session and let the agent
do everything:

```text
Install agent-capsule and wire up Claude Code by fetching and following every
step in
https://raw.githubusercontent.com/quantumpipes/agent-capsule/main/docs/tools/claude-code.md
Use `agent-capsule install claude-code` to register the hooks, then confirm.
```

## Browse your chains

The verifier is its own project,
[capsule-explorer](https://github.com/quantumpipes/capsule-explorer):

```bash
agent-capsule export --out /tmp/chains
git clone https://github.com/quantumpipes/capsule-explorer
cd capsule-explorer && npm install && npm run export && npm run dev   # http://localhost:4840
```
