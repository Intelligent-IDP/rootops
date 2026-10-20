/**
 * RootOps — TypeScript API Client
 *
 * All functions return { ok: boolean, ... } — callers never need try/catch.
 * Requests go through Next.js rewrites to the FastAPI backend (no CORS issues).
 *
 * Security: GitHub tokens are NEVER sent from the browser. The PR Review
 * endpoints proxy through the backend which holds the server-side GITHUB_TOKEN.
 */

const TIMEOUT_FAST     = 5_000;
const TIMEOUT_QUERY    = 120_000;
const TIMEOUT_INGEST   = 300_000;
const TIMEOUT_HEAL     = 120_000;
const TIMEOUT_PROFILES = 60_000;

// ── Internal fetch wrapper ──────────────────────────────────────

/** Turn raw error text (possibly JSON) into a human-readable string. */
function parseErrorText(raw: string): string {
  try {
    const parsed = JSON.parse(raw);
    // FastAPI returns {"detail": "..."} or {"detail": [{msg: "..."}]}
    if (parsed?.detail) {
      if (typeof parsed.detail === "string") return parsed.detail;
      if (Array.isArray(parsed.detail)) {
        return parsed.detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join("; ");
      }
    }
    if (parsed?.error && typeof parsed.error === "string") return parsed.error;
  } catch { /* not JSON — return raw */ }
  return raw;
}

async function api<T = Record<string, unknown>>(
  path: string,
  opts: RequestInit & { timeout?: number } = {},
): Promise<T & { ok: boolean; error?: string }> {
  const { timeout = TIMEOUT_FAST, ...init } = opts;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const res = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...init.headers },
    });
    clearTimeout(timer);

    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      // Parse FastAPI JSON error responses into readable strings
      const error = parseErrorText(text);
      return { ok: false, error } as T & { ok: false; error: string };
    }

    // 204 No Content — nothing to parse
    if (res.status === 204 || res.headers.get("content-length") === "0") {
      return { ok: true } as T & { ok: true };
    }

    const data = await res.json();
    return { ok: true, ...data };
  } catch (err: unknown) {
    clearTimeout(timer);
    if (err instanceof DOMException && err.name === "AbortError") {
      return { ok: false, error: "Request timed out" } as T & { ok: false; error: string };
    }
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, error: message } as T & { ok: false; error: string };
  }
}

// Variant for endpoints that return a bare JSON array (not an object).
async function apiList<T>(
  path: string,
  opts: RequestInit & { timeout?: number } = {},
): Promise<{ ok: boolean; data: T[]; error?: string }> {
  const { timeout = TIMEOUT_FAST, ...init } = opts;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);

  try {
    const res = await fetch(path, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...init.headers },
    });
    clearTimeout(timer);

    if (!res.ok) {
      const text = await res.text().catch(() => res.statusText);
      return { ok: false, data: [], error: text };
    }

    const data: T[] = await res.json();
    return { ok: true, data: Array.isArray(data) ? data : [] };
  } catch (err: unknown) {
    clearTimeout(timer);
    if (err instanceof DOMException && err.name === "AbortError") {
      return { ok: false, data: [], error: "Request timed out" };
    }
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, data: [], error: message };
  }
}


// ── Health & Status ─────────────────────────────────────────────

export async function getHealth() {
  return api("/health");
}

/** Deep health check — database, embedding model, LLM backend, GitHub token. */
export async function getDetailedHealth() {
  return api("/api/health/detailed", { timeout: 10_000 });
}

export async function getIngestStatus() {
  return api("/api/ingest/status");
}


// ── Code Repository Ingestion ───────────────────────────────────

export interface IngestPayload {
  repo_path?: string;
  repo_url?: string;
  branch?: string;
  max_commits?: number;
  name?: string;
  team?: string;
  tags?: string[];
  description?: string;
}

export async function triggerIngest(payload: IngestPayload) {
  return api("/api/ingest", {
    method: "POST",
    body: JSON.stringify(payload),
    timeout: TIMEOUT_INGEST,
  });
}


// ── Log Ingestion ───────────────────────────────────────────────

export async function ingestLogs(
  raw_text: string,
  service_name = "unknown",
  source = "raw",
) {
  return api("/api/ingest/logs", {
    method: "POST",
    body: JSON.stringify({ raw_text, service_name, source }),
    timeout: TIMEOUT_INGEST,
  });
}

export async function getLogStats() {
  return api("/api/ingest/logs/stats");
}

export interface SystemHealth {
  window_minutes: number;
  checked_at: string;
  sampled_events: number;
  truncated: boolean;
  status: "errors_observed" | "insufficient_evidence";
  services: { name: string; synthetic: boolean; events: number; errors: number; warnings: number; last_seen: string }[];
  evidence: { id: string; service: string; synthetic: boolean; level: string | null; timestamp: string; message: string }[];
  correlations: { request_id: string; synthetic: boolean; event_ids: string[]; basis: string }[];
}

export async function getSystemHealth() {
  return api<SystemHealth>("/api/ingest/logs/health");
}

export interface MockAnalysis {
  synthetic: true;
  request_id: string;
  mode: "llm" | "evidence_only";
  notice: string;
  analysis: string;
  timeline: { id: string; service: string; level: string | null; timestamp: string; message: string }[];
}

export async function analyzeMockScenario(event_ids: string[]) {
  return api<MockAnalysis>("/api/ingest/logs/mock-analysis", {
    method: "POST",
    body: JSON.stringify({ event_ids }),
    timeout: 45_000,
  });
}

export interface LiveAnalysis {
  synthetic: false;
  service_name: string;
  mode: "llm" | "evidence_only";
  notice: string;
  analysis: string;
  timeline: MockAnalysis["timeline"];
  sources: { repo_id: string; file_path: string; content: string; start_line: number; end_line: number; language: string | null }[];
}

export async function analyzeLiveEvidence(event_ids: string[], repo_id?: string) {
  return api<LiveAnalysis>("/api/ingest/logs/live-analysis", {
    method: "POST",
    body: JSON.stringify({ event_ids, repo_id }),
    timeout: 75_000,
  });
}

export async function getOtelReceiverStatus() {
  return api("/api/ingest/logs/otel/status");
}


// ── Query (Hybrid RAG) ─────────────────────────────────────────

export interface QueryPayload {
  question: string;
  top_k?: number;
  use_llm?: boolean;
  conversation_history?: { role: string; content: string }[];
  repo_ids?: string[];
}

export interface QuerySource {
  file_path: string;
  similarity: number;
  content: string;
  start_line?: number;
  end_line?: number;
  language?: string;
  commit_sha?: string;
  cross_referenced?: boolean;
  rerank_score?: number;
}

export interface LogMatch {
  service_name: string;
  timestamp: string;
  level: string;
  message: string;
  parsed_error?: string;
  file_reference?: string;
  similarity: number;
}

export interface QueryResult {
  ok: boolean;
  query?: string;
  answer?: string;
  sources?: QuerySource[];
  log_matches?: LogMatch[];
  metadata?: Record<string, unknown>;
  error?: string;
}

export async function queryCodebase(payload: QueryPayload): Promise<QueryResult> {
  return api<QueryResult>("/api/query", {
    method: "POST",
    body: JSON.stringify(payload),
    timeout: TIMEOUT_QUERY,
  });
}

/** @deprecated Use queryCodebase (fixes typo) */
export const queryCosebase = queryCodebase;

/**
 * Streaming RAG query — yields NDJSON events via SSE.
 *
 * Event types:
 *   { type: "metadata", data: { sources, log_matches, metadata } }
 *   { type: "token",    data: "<text chunk>" }
 *   { type: "error",    data: "<message>" }
 */
export async function* streamQuery(payload: QueryPayload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_QUERY);

  try {
    const res = await fetch("/api/query/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, use_llm: true }),
      signal: controller.signal,
    });

    clearTimeout(timer);
    if (!res.ok || !res.body) {
      yield { type: "error", data: `HTTP ${res.status}: ${await res.text().catch(() => "")}` };
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Flush complete newline-delimited JSON lines
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        try {
          yield JSON.parse(trimmed);
        } catch {
          // Skip malformed lines silently
        }
      }
    }

    // Flush remaining buffer
    if (buffer.trim()) {
      try { yield JSON.parse(buffer.trim()); } catch { /* ignore */ }
    }
  } catch (err: unknown) {
    clearTimeout(timer);
    if (err instanceof DOMException && err.name === "AbortError") {
      yield { type: "error", data: "Stream timed out" };
    } else {
      yield { type: "error", data: err instanceof Error ? err.message : "Stream connection failed" };
    }
  }
}


// ── Repositories ────────────────────────────────────────────────

export interface Repository {
  id: string;
  name: string;
  url?: string;
  local_path?: string;
  team?: string;
  tags?: string[];
  description?: string;
  chunk_count: number;
  commit_count: number;
  last_ingested_at?: string;
}

export async function getRepositories(): Promise<{ ok: boolean; repos: Repository[] }> {
  const { ok, data } = await apiList<Repository>("/api/repos");
  return { ok, repos: data };
}

export interface RepositoryArchitectureSummary {
  repo_id: string;
  repository_name: string;
  summary: string;
  chunk_count: number;
  generated_at: string;
}

export async function getRepositorySummaries() {
  return apiList<RepositoryArchitectureSummary>("/api/repos/summaries");
}

export async function getDependencyGraph(): Promise<{
  ok: boolean;
  nodes?: unknown[];
  edges?: unknown[];
  error?: string;
}> {
  return api("/api/repos/graph");
}

export interface KnowledgeGraphEntity {
  id: string;
  canonical_name: string;
  entity_type: string;
  aliases: string[];
  repos: string[];
  deprecated: boolean;
  description?: string | null;
}

export interface KnowledgeGraphEdge {
  id: string;
  source_entity: string;
  source_type: string;
  target_entity: string;
  target_type: string;
  edge_type: string;
  promotion_level: string;
  confidence: number;
  incident_count: number;
  evidence?: Record<string, unknown> | null;
}

export async function getKnowledgeGraph() {
  const [entities, edges] = await Promise.all([
    apiList<KnowledgeGraphEntity>("/api/graph/entities"),
    apiList<KnowledgeGraphEdge>("/api/graph/edges"),
  ]);
  return {
    ok: entities.ok && edges.ok,
    entities: entities.data,
    edges: edges.data,
    error: entities.error ?? edges.error,
  };
}

export interface BreadthNode {
  id: string;
  name: string;
  kind: string;
  layer?: string;
  parent?: string;
  description?: string;
  aliases?: string[];
  source?: { file?: string; line?: number; endLine?: number };
  inputs?: { name: string; type?: string }[];
  outputs?: { name: string; type?: string }[];
  metadata?: Record<string, unknown>;
}

export interface BreadthEdge {
  id?: string;
  from: { node: string; port?: string };
  to: { node: string; port?: string };
  kind: string;
  label?: string;
  protocol?: string;
  count?: number;
  metadata?: Record<string, unknown>;
}

export interface BreadthFlowGraph {
  version: string;
  nodes: BreadthNode[];
  edges: BreadthEdge[];
  insights?: { severity?: string; title: string; detail?: string; nodes: string[] }[];
  sources?: { collector: string; target?: string }[];
}

export async function getBreadthGraph(repoId: string) {
  return api<{ repo_id: string; scanned_at: string; flowgraph: BreadthFlowGraph }>(
    `/api/graph/breadth?repo_id=${encodeURIComponent(repoId)}`,
  );
}

export async function deleteRepository(repoId: string) {
  return api(`/api/repos/${repoId}`, { method: "DELETE" });
}


// ── Auto-Healing ────────────────────────────────────────────────

export async function runHeal(serviceName?: string) {
  const params = serviceName ? `?service_name=${encodeURIComponent(serviceName)}` : "";
  return api(`/api/heal${params}`, {
    method: "POST",
    timeout: TIMEOUT_HEAL,
  });
}

export interface PendingFix {
  fix_id: string;
  error_message?: string;
  error_service?: string;
  related_file?: string;
  diagnosis?: string;
  suggested_code?: string;
  similarity_score?: number;
  confidence_score?: number;
  blast_radius_level?: string;
  requires_approval?: boolean;
  auto_apply_eligible?: boolean;
  rollback_plan?: string;
}

export async function getFixes(): Promise<{ ok: boolean; fixes: PendingFix[] }> {
  const { ok, data } = await apiList<PendingFix>("/api/heal/fixes");
  return { ok, fixes: data };
}

export async function getFix(fixId: string) {
  return api(`/api/heal/fixes/${fixId}`);
}

export async function createHealPr(
  fixId: string,
  repoFullName: string,
  newContent: string,
  baseBranch = "main",
) {
  return api("/api/heal/pr", {
    method: "POST",
    body: JSON.stringify({
      fix_id: fixId,
      repo_full_name: repoFullName,
      new_content: newContent,
      base_branch: baseBranch,
    }),
    timeout: TIMEOUT_HEAL,
  });
}


// ── Developer Profiles ──────────────────────────────────────────

export interface ProfileSummary {
  author_name: string;
  author_email: string;
  commit_count: number;
  primary_languages: Record<string, number>;
  last_updated?: string;
}

export async function getProfiles(): Promise<{ ok: boolean; profiles: ProfileSummary[] }> {
  const { ok, data } = await apiList<ProfileSummary>("/api/profiles", { timeout: TIMEOUT_PROFILES });
  return { ok, profiles: data };
}

export interface ProfileDetail extends ProfileSummary {
  pattern_summary?: string;
  code_patterns?: Record<string, unknown>;
}

export async function getProfile(email: string): Promise<{ ok: boolean; error?: string } & Partial<ProfileDetail>> {
  return api(`/api/profiles/${encodeURIComponent(email)}`);
}

export async function buildProfiles() {
  return api("/api/profiles/build", {
    method: "POST",
    timeout: TIMEOUT_PROFILES,
  });
}


// ── PR Review (server-side GitHub proxy) ──────────────────────
// NOTE: No client-side GitHub token — the backend uses GITHUB_TOKEN from .env.

export interface GitHubPR {
  number: number;
  title: string;
  body?: string;
  draft?: boolean;
  user?: { login: string; avatar_url?: string };
  labels?: { name: string; color: string }[];
  additions?: number;
  deletions?: number;
  changed_files?: number;
  updated_at?: string;
  created_at?: string;
  html_url?: string;
}

export async function getPRReviewStatus() {
  return api("/api/pr-review/status");
}

export async function getPRList(
  owner: string,
  repo: string,
  token?: string,
): Promise<{ ok: boolean; prs: GitHubPR[]; rate_remaining?: string; has_token?: boolean; error?: string }> {
  const r = await api<{ prs: GitHubPR[]; rate_remaining?: string; has_token?: boolean }>(
    `/api/pr-review/prs?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}`,
    {
      timeout: 15_000,
      headers: token ? { "X-GitHub-Token": token } : {},
    },
  );
  if (!r.ok) return { ok: false, prs: [], error: r.error };
  return { ok: true, prs: r.prs ?? [], rate_remaining: r.rate_remaining, has_token: r.has_token };
}

export async function getPRDiff(
  owner: string,
  repo: string,
  prNumber: number,
  token?: string,
): Promise<{ ok: boolean; files?: unknown[]; diff?: string; error?: string }> {
  return api(
    `/api/pr-review/diff?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}&pr=${prNumber}`,
    {
      timeout: 15_000,
      headers: token ? { "X-GitHub-Token": token } : {},
    },
  );
}

/** Format a UTC ISO timestamp as a human-readable relative age string. */
export function formatAge(isoTimestamp: string): string {
  try {
    const s = Math.floor((Date.now() - new Date(isoTimestamp).getTime()) / 1000);
    if (s < 60)    return "just now";
    if (s < 3600)  return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  } catch {
    return "unknown";
  }
}
