# Capsule wire format

> The whole trust model rests on one idea: the format is open, so you never have
> to trust us. Re-implement the three checks below in any language and you will
> get the same verdict on the same chain.

This document defines the exact bytes. Any implementation that follows it
produces chains verifiable by this tool's CLI and by the in-browser explorer,
and vice versa.

## A capsule

A capsule is a JSON object with identity fields, chain-linkage fields, six
section objects, and a seal. Conceptually:

```jsonc
{
  // identity
  "id": "a3f1c2...-uuid",
  "type": "tool",            // tool | chat | agent | system (free-form string on the wire)
  "domain": "claude-code",
  "parent_id": null,

  // chain linkage
  "sequence": 7,             // 0-based position in the chain
  "previous_hash": "9c8e...",// hash of the capsule at sequence-1 (null at genesis)
  "spec_version": "1.0",

  // the six sections (each an object; keys vary by capsule type)
  "trigger":   { "type": "user_request", "source": "<session-id>", "request": "<the prompt>" },
  "context":   { "agent_id": "claude-code", "session_id": "<id>", "environment": { /* cwd, git_branch, model, usage provenance, thinking signatures, ... */ } },
  "reasoning": { "analysis": "<visible response prose>", "reasoning": "<thinking text, usually empty: redacted by Claude Code>", "model": "claude-opus-4-8" },
  "authority": { "type": "autonomous|policy", "policy_reference": "permission_mode=acceptEdits" },
  "execution": { "tool_calls": [ { "tool": "Write", "arguments": { ... }, "result": ..., "success": true, "duration_ms": 12, "error": null } ], "duration_ms": 0, "resources_used": { /* token usage */ } },
  "outcome":   { "status": "success", "summary": "Write: /path", "side_effects": ["wrote /path"], "result": ... },

  // seal (NOT part of the hashed content; see below)
  "hash": "bd7b...",
  "signature": "f0a1...",
  "signature_pq": "",
  "signed_at": "2026-05-30T20:00:01+00:00",
  "signed_by": "c6f6555b2a02b1c1"
}
```

The section bodies are intentionally permissive: keys vary by capsule type, and
verifiers must not require a fixed schema inside a section. Verification depends
only on the canonicalization and the seal, not on section contents.

## Canonical bytes (what gets hashed)

The hash is computed over the **content only**: every field above *except* the
seal fields (`hash`, `signature`, `signature_pq`, `signed_at`, `signed_by`).
Excluding the seal avoids a circular dependency (you cannot hash a field that
holds its own hash).

The canonical byte string is:

```python
json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

That is: keys sorted recursively, no whitespace, non-ASCII kept literal (UTF-8),
encoded as UTF-8. This string is stored verbatim in the export bundle as
`canonical`, so verifiers never re-serialize and can never disagree about bytes.

## Hash

```
hash = SHA3-256( canonical_bytes ).hex()      # 64 hex chars (32 bytes)
```

## Signature

The Ed25519 signature is computed over the UTF-8 bytes of the **hash hex
string**, not over the raw hash bytes:

```
signature = Ed25519_sign( signing_key, utf8(hash_hex) ).hex()   # 128 hex chars (64 bytes)
```

This is the one subtle point. The in-browser verifier matches it exactly:

```js
ed.verify(hexToBytes(signatureHex), utf8ToBytes(hashHex), hexToBytes(publicKeyHex))
```

`signature_pq` is reserved for an optional post-quantum (ML-DSA-65) signature.
This standalone build leaves it empty; verifiers treat empty as "not present".

## Chain linkage

- The genesis capsule has `sequence == 0` and `previous_hash == null`.
- Every later capsule has `sequence == prev.sequence + 1` and
  `previous_hash == prev.hash`.

## Verification algorithm

For each capsule in sequence order:

1. **hash**: `SHA3-256(canonical) == hash`
2. **signature**: `Ed25519_verify(public_key, utf8(hash), signature)`
3. **link**: `sequence == index` and `previous_hash == prior.hash`
   (`previous_hash == null` at index 0)

A chain is valid iff all three hold for every capsule. The first index where any
check fails is the break point.

## Export bundle

`agent-capsule export` (and the explorer's `npm run export`) write:

- `index.json`: `{ generated_at, public_key, fingerprint, chain_count, capsule_count, chains: [summary...] }`
- `<chain-id>.json`: `{ id, title, length, head_hash, genesis_hash, all_hashes_ok, capsules: [ { hash, signature, signature_pq, signed_at, signed_by, canonical } ] }`

Every display field (id, type, sequence, sections, ...) is parsed from
`canonical` client-side, so the bundle ships each byte exactly once.
