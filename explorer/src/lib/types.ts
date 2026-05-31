// Shapes mirror the exporter (scripts/export_chains.py) and the qp-capsule
// six-section model. Kept intentionally permissive (Record) on section bodies
// since capsule contents vary by capsule type.

export type CapsuleType =
  | "agent"
  | "tool"
  | "chat"
  | "system"
  | "workflow"
  | "vault"
  | "auth"
  | "kill"
  | string;

export interface ToolCall {
  tool: string;
  arguments?: unknown;
  result?: unknown;
  success?: boolean;
  duration_ms?: number | null;
  error?: string | null;
}

export interface CapsuleExec {
  tool_calls?: ToolCall[];
  duration_ms?: number;
  resources_used?: Record<string, unknown>;
}

export interface Capsule {
  id: string;
  type: CapsuleType;
  domain: string;
  sequence: number;
  parent_id: string | null;
  previous_hash: string | null;
  hash: string;
  signature: string;
  signature_pq?: string;
  signed_at?: string | null;
  signed_by?: string;
  spec_version?: string;
  trigger: Record<string, unknown>;
  context: Record<string, unknown>;
  reasoning: Record<string, unknown>;
  authority: Record<string, unknown>;
  execution: CapsuleExec;
  outcome: Record<string, unknown>;
  // crypto material for in-browser verification
  canonical: string;
  recomputed_hash: string;
  hash_ok: boolean;
}

// What the exporter actually ships per capsule: seal fields + canonical bytes.
// Everything else (sections, id, type, sequence, previous_hash) is parsed from
// `canonical` client-side to keep the bundle small.
export interface RawCapsule {
  hash: string;
  signature: string;
  signature_pq?: string;
  signed_at?: string | null;
  signed_by?: string;
  canonical: string;
}

export interface RawChain extends ChainSummary {
  db_path?: string;
  capsules: RawCapsule[];
}

export interface ChainSummary {
  id: string;
  title: string;
  length: number;
  head_hash: string;
  genesis_hash: string;
  all_hashes_ok: boolean;
}

export interface Chain extends ChainSummary {
  db_path?: string;
  capsules: Capsule[];
}

export interface ChainIndex {
  generated_at: string;
  public_key: string;
  fingerprint: string;
  chain_count: number;
  capsule_count: number;
  chains: ChainSummary[];
}

// Per-capsule verification result computed client-side.
export type VerifyState = "idle" | "running" | "ok" | "fail";

export interface CapsuleVerdict {
  sequence: number;
  id: string;
  hashOk: boolean; // recomputed SHA3-256 === stored hash
  sigOk: boolean; // Ed25519 verify over the hash
  linkOk: boolean; // previous_hash matches prior capsule's hash + sequence consecutive
  ok: boolean;
}

export interface ChainVerdict {
  valid: boolean;
  verified: number;
  brokenAt: number | null;
  results: CapsuleVerdict[];
}
