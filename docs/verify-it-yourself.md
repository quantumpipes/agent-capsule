# Verify it yourself

The entire point of agent-capsule is that **you never have to trust it**. The
format is open, the crypto is standard, and a chain carries everything a third
party needs to check it: the exact bytes that were hashed, the hash, the
signature, and the public key. Re-implement the three checks below in any
language and you will reach the same verdict on the same chain.

This page shows you how, three ways: with the built-in tools, from scratch in
Python, and from scratch in JavaScript.

## What you are checking

For every capsule in a chain, in order:

1. **Hash.** `SHA3-256` of the capsule's canonical bytes equals the stored `hash`.
2. **Signature.** `Ed25519` verifies the `signature` over the UTF-8 of the `hash`
   hex string, against the chain's public key.
3. **Link.** `sequence` is consecutive from 0, and `previous_hash` equals the
   previous capsule's `hash` (null at genesis).

If all three hold for every capsule, the chain is intact. The first capsule where
any check fails is the exact point of tampering.

## Get a chain to check

Export your chains to a JSON bundle (each capsule carries the canonical bytes and
the bundle carries the public key):

```bash
agent-capsule export --out /tmp/chains
ls /tmp/chains            # index.json + one <tool>-<session>.json per chain
```

`index.json` holds the `public_key` (hex). Each `<chain>.json` holds a `capsules`
array; each capsule has `hash`, `signature`, and `canonical` (the exact bytes
that were hashed).

## Way 1: the built-in verifier

```bash
agent-capsule verify ~/.agent-capsule/chains/cursor/a3f1c2-checkout.db --signatures
# [OK] a3f1c2-checkout.db: 64/64 verified (head 9c8ec070...)
```

The browser [Capsule Explorer](https://github.com/quantumpipes/capsule-explorer)
does the same thing client-side. Useful, but it is still our code. The next two
ways use none of our code.

## Way 2: from scratch in Python

Standard library plus PyNaCl (or any Ed25519 library). This reads the exported
bundle and trusts nothing but `hashlib` and the signature math.

```python
import hashlib, json, sys
from pathlib import Path
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError

bundle = Path(sys.argv[1])                      # e.g. /tmp/chains
public_key = json.loads((bundle / "index.json").read_text())["public_key"]
vk = VerifyKey(bytes.fromhex(public_key))

for chain_file in bundle.glob("*.json"):
    if chain_file.name == "index.json":
        continue
    chain = json.loads(chain_file.read_text())
    prev = None
    ok = True
    for i, cap in enumerate(chain["capsules"]):
        canonical = cap["canonical"].encode("utf-8")
        # 1. hash
        if hashlib.sha3_256(canonical).hexdigest() != cap["hash"]:
            print(f"{chain['id']}: HASH MISMATCH at #{i}"); ok = False; break
        # 2. signature (Ed25519 over the UTF-8 of the hash hex string)
        try:
            vk.verify(cap["hash"].encode("utf-8"), bytes.fromhex(cap["signature"]))
        except BadSignatureError:
            print(f"{chain['id']}: BAD SIGNATURE at #{i}"); ok = False; break
        # 3. link
        d = json.loads(cap["canonical"])
        expected_prev = None if i == 0 else prev
        if d["sequence"] != i or d["previous_hash"] != expected_prev:
            print(f"{chain['id']}: BROKEN LINK at #{i}"); ok = False; break
        prev = cap["hash"]
    if ok:
        print(f"{chain['id']}: OK ({len(chain['capsules'])} capsules)")
```

```bash
python3 verify_from_scratch.py /tmp/chains
# cursor-a3f1c2-checkout: OK (64 capsules)
```

## Way 3: from scratch in JavaScript

Using the audited [`@noble`](https://github.com/paulmillr/noble-hashes)
primitives, the same ones the explorer uses. Runs in Node or a browser.

```js
import { sha3_256 } from "@noble/hashes/sha3";
import { sha512 } from "@noble/hashes/sha512";
import { bytesToHex, hexToBytes, utf8ToBytes } from "@noble/hashes/utils";
import * as ed from "@noble/ed25519";
ed.etc.sha512Sync = (...m) => sha512(ed.etc.concatBytes(...m));   // enable sync verify

function verifyChain(chain, publicKeyHex) {
  let prev = null;
  for (let i = 0; i < chain.capsules.length; i++) {
    const cap = chain.capsules[i];
    // 1. hash
    if (bytesToHex(sha3_256(utf8ToBytes(cap.canonical))) !== cap.hash) return { ok: false, brokenAt: i };
    // 2. signature
    if (!ed.verify(hexToBytes(cap.signature), utf8ToBytes(cap.hash), hexToBytes(publicKeyHex))) return { ok: false, brokenAt: i };
    // 3. link
    const d = JSON.parse(cap.canonical);
    const expectedPrev = i === 0 ? null : prev;
    if (d.sequence !== i || d.previous_hash !== expectedPrev) return { ok: false, brokenAt: i };
    prev = cap.hash;
  }
  return { ok: true, verified: chain.capsules.length };
}
```

## Prove it breaks

Verification is only meaningful if it catches tampering. Edit one byte of a
capsule's `canonical` text in a chain (or in the exported JSON) and re-run any of
the three verifiers. Every one of them breaks at exactly that capsule, because
its recomputed hash no longer matches, which also breaks the `previous_hash` of
every capsule after it.

```bash
# flip one byte in the stored canonical of the genesis capsule, then:
agent-capsule verify ~/.agent-capsule/chains/cursor/a3f1c2-checkout.db --signatures
# [BROKEN] a3f1c2-checkout.db: 0/64 verified (broken at seq 0: content hash mismatch at 0)
```

## Why this matters

You can hand anyone your chain JSON plus the public key. With nothing but a
SHA3-256 and an Ed25519 implementation, in any language, on an air-gapped
machine, they can confirm that the record is exactly what your agent produced and
that no one has touched it since. You are not asking them to trust you. You are
handing them the proof and the means to check it.

## See also

- [wire-format.md](wire-format.md): the exact canonical bytes and signature scheme.
- [data-model.md](data-model.md): what is inside a capsule.
- [threat-model.md](threat-model.md): what this does and does not protect against.
