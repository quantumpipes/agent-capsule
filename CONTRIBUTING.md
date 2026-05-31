# Contributing to agent-capsule

Thanks for helping seal AI coding sessions into chains anyone can verify. This
guide gets you set up and explains the conventions that keep the project tight.

## Philosophy

agent-capsule is one shared crypto **engine** plus one thin **adapter** per tool.
The engine owns everything load-bearing: building the six-section capsule,
canonical hashing (SHA3-256), Ed25519 signing, chain linking, idempotent
re-sealing, and verification. An adapter knows exactly two things: the tool's
lifecycle trigger, and how to read that tool's transcript into a plain spec dict
per action. Keep adapters thin and the crypto shared, and every tool produces the
same kind of chain that one explorer verifies byte for byte. See
[docs/architecture.md](docs/architecture.md) and
[docs/writing-an-adapter.md](docs/writing-an-adapter.md).

## Project layout

```
src/agent_capsule/
  core/        the shared engine: capsule, seal, storage, chain, sealing, paths, export
  adapters/    one thin parser per tool: claude_code.py, cursor.py, codex.py, cline.py
  cli.py       the umbrella CLI: verify / inspect / list / install / uninstall / export
tests/         the test suite
docs/          architecture, wire format, per-tool guides
```

## Dev setup

```bash
git clone https://github.com/quantumpipes/agent-capsule
cd agent-capsule
pip install -e ".[dev]"

# Run the tests:
PYTHONPATH=src python3 -m pytest tests/
```

Python 3.11+ is required. The only runtime dependency is PyNaCl; the dev extra
adds pytest.

## Adding a tool

Adding support for a new agent is writing one parser, not a new crypto stack. Read
[docs/writing-an-adapter.md](docs/writing-an-adapter.md) for the full walkthrough:
install the trigger, parse the transcript into specs, and call
`core.sealing.seal_specs(tool, session, specs)`. The engine does the rest.

## Conventions

- **Type hints** on all function signatures.
- **Docstrings** with Args/Returns/Raises on public functions.
- **Fail-open adapters.** An adapter logs to `~/.agent-capsule/hook.log` and always
  exits cleanly. An audit tool must never be able to break the thing it audits.
- **Keep adapters thin.** Anything cryptographic, anything shared across tools,
  belongs in `core`, not in an adapter.
- **No em dashes or en dashes anywhere in the docs.** A commit hook rejects them.
  Use commas, colons, periods, semicolons, or parentheses instead.
- **Conventional-commit messages.** Prefix with `feat`, `fix`, `docs`, `refactor`,
  or `test`. Scope in parentheses where it helps, for example
  `feat(adapters): add windsurf adapter`.
- Say "open source," not "free."

## Security issues

Do not open a public issue for a suspected vulnerability. Follow the process in
[SECURITY.md](SECURITY.md): open a private security advisory on the GitHub
repository, or email the address listed in the repository profile.

## License

agent-capsule is licensed under Apache-2.0. By contributing, you agree that your
contributions are licensed under the same terms. Copyright 2026 Quantum Pipes
Technologies, LLC.
