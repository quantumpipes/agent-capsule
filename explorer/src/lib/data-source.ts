// Data source for the explorer. Two modes (chosen at build/runtime):
//
//  static (default) — fetch the JSON the exporter wrote to /data/chains/.
//                     Air-gapped, no backend, ships with the static build.
//  live (PUBLIC_CAPSULE_API set to a Core base URL): the explorer
//                     then reads /v1/capsules* and verifies via the server.
//                     (Client-side crypto verify needs canonical bytes, which
//                     the static export carries; live mode falls back to the
//                     server's /v1/capsules/verify-chain.)
//
// Both modes resolve to the same Chain / ChainIndex shapes so the UI is mode-agnostic.

import type { Capsule, Chain, ChainIndex, RawCapsule, RawChain } from "./types";
import { loadLiveChain, loadLiveIndex, serverVerifyChain, type ServerVerifyResult } from "./live-source";

const STATIC_BASE = "/data/chains";
const LIVE_BASE = (import.meta.env.PUBLIC_CAPSULE_API as string | undefined)?.replace(/\/$/, "");

export const MODE: "static" | "live" = LIVE_BASE ? "live" : "static";

/** Parse a raw capsule: every display field lives inside `canonical`. */
export function parseCapsule(raw: RawCapsule): Capsule {
  let d: Record<string, unknown> = {};
  try {
    d = JSON.parse(raw.canonical) as Record<string, unknown>;
  } catch {
    d = {};
  }
  const sec = (k: string) => (d[k] && typeof d[k] === "object" ? (d[k] as Record<string, unknown>) : {});
  return {
    id: String(d.id ?? raw.hash.slice(0, 16)),
    type: String(d.type ?? "unknown"),
    domain: String(d.domain ?? ""),
    sequence: Number(d.sequence ?? 0),
    parent_id: (d.parent_id as string | null) ?? null,
    previous_hash: (d.previous_hash as string | null) ?? null,
    hash: raw.hash,
    signature: raw.signature,
    signature_pq: raw.signature_pq,
    signed_at: raw.signed_at,
    signed_by: raw.signed_by,
    trigger: sec("trigger"),
    context: sec("context"),
    reasoning: sec("reasoning"),
    authority: sec("authority"),
    execution: sec("execution") as Capsule["execution"],
    outcome: sec("outcome"),
    canonical: raw.canonical,
    recomputed_hash: "",
    hash_ok: false,
  };
}

export async function loadIndex(): Promise<ChainIndex> {
  if (LIVE_BASE) return loadLiveIndex(LIVE_BASE);
  const res = await fetch(`${STATIC_BASE}/index.json`, { cache: "no-cache" });
  if (!res.ok) throw new Error(`Could not load chain index (${res.status}). Run: npm run export`);
  return (await res.json()) as ChainIndex;
}

export async function loadChain(id: string): Promise<Chain> {
  if (LIVE_BASE) return loadLiveChain(LIVE_BASE, id);
  const res = await fetch(`${STATIC_BASE}/${encodeURIComponent(id)}.json`, { cache: "no-cache" });
  if (!res.ok) throw new Error(`Could not load chain ${id} (${res.status}).`);
  const raw = (await res.json()) as RawChain;
  return { ...raw, capsules: raw.capsules.map(parseCapsule) };
}

/** Live-mode verification, delegated to the Core server. */
export async function verifyOnServer(): Promise<ServerVerifyResult> {
  if (!LIVE_BASE) throw new Error("verifyOnServer is only available in live mode");
  return serverVerifyChain(LIVE_BASE);
}
