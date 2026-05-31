# Capsule Explorer

Navigate and **cryptographically verify** capsule hashchains, in the browser,
offline. Built with Astro + React islands, Tailwind, and self-hosted fonts.

The headline capability: the page **re-verifies the entire chain in your
browser, with no backend**. It recomputes SHA3-256 over each capsule's canonical
bytes and verifies the Ed25519 signature (`@noble/hashes` + `@noble/ed25519`),
then checks `previous_hash` linkage and sequence order. No network, no trust
required.

## Data flow

```
~/.claude-capsule/chains/*.db          (per-conversation chains, written by the
   |                                     claude-capsule hook)
   v
scripts/export_chains.py               (reads SQLite -> JSON; includes the exact
   |                                     canonical bytes + the Ed25519 public key)
   v
public/data/chains/index.json          (chain summaries + public key)
public/data/chains/<chain-id>.json     (per chain, loaded on demand)
   v
src/lib/data-source.ts -> src/lib/crypto.ts (in-browser verify) -> Explorer.tsx
```

## Run

```bash
npm install
npm run export      # regenerate public/data/chains from local chain DBs
npm run dev         # http://localhost:4840
```

`npm run export` runs `python3 scripts/export_chains.py`, which needs Python 3
with PyNaCl available (`pip install PyNaCl`). It reads
`~/.claude-capsule/chains/*.db` by default; pass `--db PATH` or `--glob PAT` to
point elsewhere.

## Tests

```bash
npm run build && npm test
```

- `verify.test.ts`: recomputes SHA3-256 + Ed25519 over the real exported chains,
  asserts every chain verifies, a one-byte tamper breaks at the exact index, and
  a wrong public key is rejected.
- `build-integrity.test.ts`: asserts the discovery/security surface, CSP +
  immutable caching, canonical + JSON-LD + OG.

## Layout

App shell (not a marketing page): a slim top bar over a full-height three-pane
master-detail view (chains rail, capsule timeline, capsule detail). Mobile
collapses to a chain `<select>` plus a slide-over detail panel. In-chain search +
type filters, deep-linking (`?chain=id`), copy-hash, and scroll-to-break on
tamper.

## Static vs live

- **static** (default): the SPA fetches the exported JSON. Air-gapped, ships with
  the build.
- **live**: set `PUBLIC_CAPSULE_API` to a capsule server's base URL to read
  `/v1/capsules*`. Optional; the static path is the canonical one for this repo.
