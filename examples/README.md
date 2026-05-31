# Examples

`example-session.jsonl` is a synthetic Claude Code transcript: one prompt, a
`Write`, a `Bash` run, and a final answer. Use it to see a chain built and
verified without waiting for a real session.

```bash
# From the repo root, with the package importable (pip install -e . or PYTHONPATH=src):

# 1. Seal the example transcript into a chain (Claude Code adapter)
python3 -m agent_capsule.adapters.claude_code --transcript examples/example-session.jsonl --session example --finalize

# 2. Verify it (recompute hashes, links, and Ed25519 signatures)
agent-capsule verify ~/.agent-capsule/chains/claude-code/example.db --signatures

# 3. List every chain, grouped by tool
agent-capsule list

# 4. Print one capsule in full
agent-capsule inspect ~/.agent-capsule/chains/claude-code/example.db --seq 0

# 5. Export the explorer bundle (then browse with the capsule-explorer repo)
agent-capsule export --out /tmp/chains
```

Expected: three capsules (two `tool`, one `chat`), all verifying, with a head
hash printed. The assistant's first text ("I'll create greet.py...") folds into
its `Write` capsule's reasoning rather than becoming its own capsule, because a
turn with a tool call is recorded as a tool action. Try editing one byte of a
capsule's stored canonical text in the SQLite file, then re-run `verify`: it
breaks at the exact sequence.

The other adapters parse their own tool's transcript the same way; see
[../docs/tools/](../docs/tools/) for each.
