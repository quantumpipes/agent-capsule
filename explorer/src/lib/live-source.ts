// Live data source: read capsules from a running Core instance.
//
// Core serves ONE global (or per-tenant) chain via /v1/capsules; it does not
// expose the canonical bytes or the signing public key, so the browser cannot
// re-run SHA3-256 + Ed25519 itself. In live mode we therefore:
//   - group capsules by context.session_id into per-conversation "chains",
//   - populate the six display sections from the capsule detail,
//   - delegate verification to Core's own /v1/capsules/verify-chain endpoint.
//
// Static mode (the default) keeps full in-browser crypto verification.

import type { Capsule, Chain, ChainIndex, ChainSummary } from "./types";

interface CoreSummary {
  id: string;
  type?: string;
  domain?: string;
  sequence?: number;
  session_id?: string | null;
  previous_hash?: string | null;
  hash?: string;
  trigger_request?: string;
  outcome_summary?: string;
  outcome_status?: string;
  agent_id?: string;
  sealed?: boolean;
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { accept: "application/json" }, credentials: "include" });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return (await res.json()) as T;
}

function listOf(payload: Record<string, unknown>): CoreSummary[] {
  const arr = (payload.capsules ?? payload.items ?? []) as CoreSummary[];
  return Array.isArray(arr) ? arr : [];
}

/** Fetch all capsules (paged) and group them into chains by session_id. */
async function fetchAll(base: string): Promise<CoreSummary[]> {
  const out: CoreSummary[] = [];
  const pageSize = 500;
  for (let offset = 0; offset < 20000; offset += pageSize) {
    const payload = await getJSON<Record<string, unknown>>(`${base}/v1/capsules?limit=${pageSize}&offset=${offset}`);
    const page = listOf(payload);
    out.push(...page);
    if (page.length < pageSize) break;
  }
  return out;
}

function groupChains(caps: CoreSummary[]): Map<string, CoreSummary[]> {
  const groups = new Map<string, CoreSummary[]>();
  for (const c of caps) {
    const key = c.session_id || "ungrouped";
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(c);
  }
  for (const list of groups.values()) list.sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
  return groups;
}

export async function loadLiveIndex(base: string): Promise<ChainIndex> {
  const caps = await fetchAll(base);
  const groups = groupChains(caps);
  const chains: ChainSummary[] = [];
  for (const [id, list] of groups) {
    chains.push({
      id,
      title: list.find((c) => c.trigger_request)?.trigger_request?.slice(0, 90) || id,
      length: list.length,
      head_hash: list[list.length - 1]?.hash ?? "",
      genesis_hash: list[0]?.hash ?? "",
      all_hashes_ok: list.every((c) => c.sealed !== false),
    });
  }
  chains.sort((a, b) => b.length - a.length);
  return {
    generated_at: new Date().toISOString(),
    public_key: "", // not exposed by Core; client crypto verify is unavailable in live mode
    fingerprint: "core",
    chain_count: chains.length,
    capsule_count: caps.length,
    chains,
  };
}

function toCapsule(s: CoreSummary, i: number): Capsule {
  return {
    id: s.id,
    type: s.type ?? "unknown",
    domain: s.domain ?? "",
    sequence: s.sequence ?? i,
    parent_id: null,
    previous_hash: s.previous_hash ?? null,
    hash: s.hash ?? "",
    signature: "",
    signed_by: "core",
    trigger: { request: s.trigger_request, source: s.session_id },
    context: { agent_id: s.agent_id, session_id: s.session_id },
    reasoning: {},
    authority: {},
    execution: {},
    outcome: { status: s.outcome_status, summary: s.outcome_summary },
    canonical: "", // not available live -> client crypto cannot run
    recomputed_hash: "",
    hash_ok: Boolean(s.sealed),
  };
}

export async function loadLiveChain(base: string, sessionId: string): Promise<Chain> {
  const caps = (await fetchAll(base)).filter((c) => (c.session_id || "ungrouped") === sessionId);
  caps.sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0));
  const capsules = caps.map(toCapsule);
  return {
    id: sessionId,
    title: caps.find((c) => c.trigger_request)?.trigger_request?.slice(0, 90) || sessionId,
    length: capsules.length,
    head_hash: capsules[capsules.length - 1]?.hash ?? "",
    genesis_hash: capsules[0]?.hash ?? "",
    all_hashes_ok: capsules.every((c) => c.hash_ok),
    capsules,
  };
}

export interface ServerVerifyResult {
  valid: boolean;
  capsules_verified: number;
  error: string | null;
  broken_at: string | null;
}

/** Live verification is delegated to Core's own endpoint. */
export async function serverVerifyChain(base: string): Promise<ServerVerifyResult> {
  const res = await fetch(`${base}/v1/capsules/verify-chain`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    credentials: "include",
  });
  if (!res.ok) throw new Error(`verify-chain -> ${res.status}`);
  return (await res.json()) as ServerVerifyResult;
}
