---
name: Bug report
about: Something does not work as expected
title: ""
labels: bug
assignees: ""
---

> Do not paste private capsule contents, chain databases, exported bundles, or any
> key material. Those record your real sessions and your signing key. Share only the
> log lines and minimal details below.

## Which agent / tool

Claude Code, Cursor, Codex, or Cline (and the tool's version if you have it).

## agent-capsule version

Output of `agent-capsule --version` (or the version from pyproject.toml).

## What happened

A clear description of the actual behavior.

## What you expected

What you expected to happen instead.

## Relevant hook.log lines

The relevant lines from `~/.agent-capsule/hook.log`. Adapters are fail-open and log
there, so this is usually where the cause shows up. Redact anything sensitive.

```
paste log lines here
```

## Reproduction

The steps to reproduce, as minimal as you can make them.

## Environment

- OS:
- Python version:
