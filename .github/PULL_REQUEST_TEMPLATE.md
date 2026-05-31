## Summary

What this change does, and why.

## Which tool(s) it affects

Claude Code, Cursor, Codex, Cline, the shared core, the CLI, or docs only.

## Checklist

- [ ] Tests added or updated, and the suite passes (`PYTHONPATH=src python3 -m pytest tests/`).
- [ ] Docs updated to match the change (zero drift: code and docs in the same PR).
- [ ] No em dashes or en dashes in any docs (a commit hook rejects them).
- [ ] Adapters stay thin: nothing cryptographic or shared moved into an adapter.
- [ ] Adapters remain fail-open (log and exit cleanly, never block the agent).
- [ ] Commit messages follow conventional style (feat / fix / docs / refactor / test).
