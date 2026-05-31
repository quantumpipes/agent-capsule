# Anatomy of a capsule

> Six sections. One truth. Tamper-evident forever.

A **capsule** is one cryptographically sealed record of one action your agent
took. Not a log line, not a chat bubble: a structured record that answers the
six questions you would ask in any audit, then is hashed, signed, and linked to
the action before it.

Every action your agent takes becomes one capsule. Every capsule tells the whole
story of that action through six sections, carries its own cryptographic seal,
and points back at the previous capsule, forming an unbroken chain.

```
            ┌──────────────────────────────────────────────┐
            │  CAPSULE  (one action)                         │
            │                                                │
   what ───▶│  1. TRIGGER     what initiated this action     │
  state ───▶│  2. CONTEXT     the state of the world         │
    why ───▶│  3. REASONING   why this decision was made     │
    who ───▶│  4. AUTHORITY   who or what approved it         │
   what ───▶│  5. EXECUTION   what actually happened          │
 result ───▶│  6. OUTCOME     the result and side effects     │
            │  ───────────────────────────────────────────  │
            │  SEAL           SHA3-256 hash + Ed25519 sig    │
            │  LINK           sequence + previous_hash        │
            └──────────────────────────────────────────────┘
```

## The six sections

Each section is a plain JSON object. The keys below are the conventional ones;
sections are intentionally permissive (a tool can add keys), and verification
never depends on section contents, only on the canonical bytes and the seal.

### 1. Trigger: what initiated this action

The origin of the action: who or what asked for it, when, and what was asked.

```jsonc
"trigger": {
  "type": "user_request",          // user_request | scheduled | system | agent
  "source": "<session-id>",        // who/what triggered it
  "request": "Refactor the checkout flow to use the new pricing service"
}
```

### 2. Context: the state of the world

The environment the action ran in: which agent, which session, and provenance
(working directory, git branch, model, timestamps).

```jsonc
"context": {
  "agent_id": "cursor",
  "session_id": "a3f1c2-checkout",
  "environment": { "cwd": "/Users/dev/app", "git_branch": "main", "model": "claude-opus-4-8" }
}
```

### 3. Reasoning: why this decision was made

The visible deliberation. `analysis` holds the agent's visible prose; `reasoning`
holds hidden thinking text when a tool exposes it (often empty, since some tools
redact it and keep only a proof-of-reasoning signature).

```jsonc
"reasoning": {
  "analysis": "The checkout flow calls the legacy pricing path; I'll route it through PricingService.quote().",
  "reasoning": "",
  "model": "claude-opus-4-8"
}
```

### 4. Authority: who or what approved it

The governance posture. Did the agent act on its own, under a policy, or with a
human approval? This is how you see, after the fact, when an agent acted
autonomously versus with a gate.

```jsonc
"authority": {
  "type": "autonomous",            // autonomous | policy | human_approved | escalated
  "policy_reference": "permission_mode=acceptEdits"
}
```

### 5. Execution: what actually happened

The concrete actions: the tool calls, their arguments, their results, timing, and
resources used (token usage).

```jsonc
"execution": {
  "tool_calls": [
    { "tool": "edit_file_v2", "arguments": { "file_path": "src/checkout/flow.ts" },
      "result": "ok", "success": true, "duration_ms": 40, "error": null }
  ],
  "duration_ms": 40,
  "resources_used": { "input_tokens": 800, "output_tokens": 120 }
}
```

### 6. Outcome: the result and side effects

What came of it: status, a human-readable summary, side effects, and the full
result payload.

```jsonc
"outcome": {
  "status": "success",             // pending | success | failure | partial | blocked
  "summary": "edit_file_v2: src/checkout/flow.ts",
  "side_effects": ["wrote src/checkout/flow.ts"],
  "result": "..."
}
```

## The seal

After the six sections (plus the identity and link fields) are fixed, the capsule
is sealed:

```jsonc
"hash":      "f379ba5ce838ad05...",   // SHA3-256 over the capsule's canonical bytes
"signature": "2a57caf40e90ca6a...",   // Ed25519 signature over the hash hex string
"signature_pq": "",                    // reserved for an optional post-quantum signature
"signed_at": "2026-05-31T00:00:00+00:00",
"signed_by": "153405388f063b60"        // public-key fingerprint
```

The seal fields are **not** part of what gets hashed (you cannot hash a field
that holds its own hash). The exact bytes that are hashed, and the precise
signature scheme, are pinned in [wire-format.md](wire-format.md).

## The link

Two fields turn a pile of capsules into a tamper-evident chain:

```jsonc
"sequence": 1,                  // 0-based position in the chain
"previous_hash": "41f0ef48..."  // the hash of the capsule at sequence - 1 (null at genesis)
```

Change any byte of any capsule and its `hash` changes, which no longer matches
the next capsule's `previous_hash`, which breaks the chain at exactly that point.
That is the whole guarantee, and you can check it yourself: see
[verify-it-yourself.md](verify-it-yourself.md).

## A full capsule

```jsonc
{
  "id": "a3f1c2e9-...-uuid",
  "type": "tool",                 // tool | chat | agent | system
  "domain": "cursor",             // the tool that produced it
  "parent_id": null,
  "sequence": 1,
  "previous_hash": "41f0ef48...",
  "spec_version": "1.0",

  "trigger":   { "type": "user_request", "source": "a3f1c2-checkout", "request": "Refactor the checkout flow..." },
  "context":   { "agent_id": "cursor", "session_id": "a3f1c2-checkout", "environment": { "cwd": "/Users/dev/app", "model": "claude-opus-4-8" } },
  "reasoning": { "analysis": "Route checkout through PricingService.quote()", "reasoning": "", "model": "claude-opus-4-8" },
  "authority": { "type": "autonomous", "policy_reference": "permission_mode=acceptEdits" },
  "execution": { "tool_calls": [ { "tool": "edit_file_v2", "arguments": { "file_path": "src/checkout/flow.ts" }, "result": "ok", "success": true, "duration_ms": 40, "error": null } ], "duration_ms": 40, "resources_used": { "input_tokens": 800, "output_tokens": 120 } },
  "outcome":   { "status": "success", "summary": "edit_file_v2: src/checkout/flow.ts", "side_effects": ["wrote src/checkout/flow.ts"], "result": "ok" },

  "hash": "f379ba5c...", "signature": "2a57caf4...", "signature_pq": "",
  "signed_at": "2026-05-31T00:00:00+00:00", "signed_by": "153405388f063b60"
}
```

Open any capsule in the [Capsule Explorer](https://github.com/quantumpipes/capsule-explorer)
to see these six sections rendered, with each section's seal re-verified live in
your browser.

## See also

- [wire-format.md](wire-format.md): the exact canonical bytes, hash, and signature scheme.
- [verify-it-yourself.md](verify-it-yourself.md): re-derive the hash and check the signature in any language.
- [architecture.md](architecture.md): how each tool's transcript becomes capsules.
