"use client";

import { useEffect, useState } from "react";
import { PageHeader, Card, Button, StatusBadge, EmptyState, SectionTitle } from "@/components/ui";
import { SimilarityBar, RiskScoreBar } from "@/components/charts";
import { Markdown } from "@/lib/markdown";
import {
  getPRList,
  getPRDiff,
  getPRReviewStatus,
  queryCodebase,
  formatAge,
  type GitHubPR,
  type QueryResult,
} from "@/lib/api";
import { ArrowLeft, GitPullRequest, ShieldCheck, ShieldAlert, Eye, EyeOff } from "lucide-react";

const DIMENSIONS = [
  { label: "Code Similarity",    prompt: "What code in the codebase is most similar to these changes?" },
  { label: "Known Bug Patterns", prompt: "Do these code changes introduce bugs, race conditions, or error-prone patterns?" },
  { label: "Idempotency",        prompt: "Could these changes cause duplicate operations or break idempotency?" },
  { label: "Data Integrity",     prompt: "Do these changes risk data consistency or incomplete rollbacks?" },
  { label: "Security",           prompt: "Are there security vulnerabilities — injection risks, auth bypasses, exposed secrets?" },
];

const INPUT_CLS =
  "w-full px-3.5 py-2.5 bg-bg-input border border-white/[0.09] rounded-xl text-[13px] text-text placeholder:text-text-dim focus:border-accent/40 focus:ring-2 focus:ring-accent/[0.07] outline-none transition-all";

export default function PRReviewPage() {
  const [ownerRepo, setOwnerRepo]   = useState("");
  const [prs, setPrs]               = useState<GitHubPR[]>([]);
  const [loading, setLoading]       = useState(false);
  const [error, setError]           = useState("");
  const [selectedPR, setSelectedPR] = useState<GitHubPR | null>(null);
  const [analysing, setAnalysing]   = useState(false);
  const [analysis, setAnalysis]     = useState<{
    dimensions: Record<string, QueryResult>;
    riskScore: number;
  } | null>(null);
  const [topK, setTopK]             = useState(4);
  const [tokenStatus, setTokenStatus] = useState<{ has_token: boolean; note: string } | null>(null);
  const [githubToken, setGithubToken] = useState("");
  const [showToken, setShowToken]     = useState(false);

  // Fetch server-side token status on mount (no secret sent to browser)
  useEffect(() => {
    getPRReviewStatus().then((r) => {
      if (r.ok) {
        setTokenStatus({
          has_token: Boolean(r.has_token),
          note: String(r.note ?? ""),
        });
      }
    }).catch(() => {});
  }, []);

  async function fetchPRs() {
    const parts = ownerRepo.trim().split("/");
    if (parts.length < 2 || !parts[0] || !parts[1]) {
      setError("Enter a valid owner/repo (e.g. facebook/react)");
      return;
    }
    const [owner, repo] = parts;
    setLoading(true);
    setError("");
    const res = await getPRList(owner, repo, githubToken || undefined);
    if (res.ok) setPrs(res.prs);
    else        setError(res.error || "Failed to fetch PRs");
    setLoading(false);
  }

  async function analyse(pr: GitHubPR) {
    const parts = ownerRepo.trim().split("/");
    if (parts.length < 2) return;
    const [owner, repo] = parts;

    setSelectedPR(pr);
    setAnalysing(true);
    setAnalysis(null);

    try {
      const diffRes = await getPRDiff(owner, repo, pr.number, githubToken || undefined);
      if (!diffRes.ok) {
        setError(diffRes.error || "Failed to fetch PR diff");
        setSelectedPR(null);
        return;
      }

      const diffExcerpt = (diffRes.diff ?? "").slice(0, 2000);

      const settled = await Promise.allSettled(
        DIMENSIONS.map((dim) =>
          queryCodebase({ question: `${dim.prompt}\n\n${diffExcerpt}`, top_k: topK, use_llm: true }),
        ),
      );

      const results: Record<string, QueryResult> = {};
      DIMENSIONS.forEach((dim, i) => {
        const r = settled[i];
        results[dim.label] = r.status === "fulfilled" ? r.value : { ok: false };
      });

      const allSims = Object.values(results).flatMap((r) =>
        (r.sources ?? []).map((s) => s.similarity),
      );
      const raw = allSims.length
        ? Math.max(...allSims) * 60 + (allSims.reduce((a, b) => a + b, 0) / allSims.length) * 40
        : 0;

      setAnalysis({ dimensions: results, riskScore: Math.min(100, raw) });
    } finally {
      setAnalysing(false);
    }
  }

  // ── Analysis view ─────────────────────────────────────────────
  if (selectedPR) {
    return (
      <>
        <PageHeader
          title="PR Review"
          subtitle="Semantic risk analysis on open pull requests"
          action={
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => { setSelectedPR(null); setAnalysis(null); setError(""); }}
            >
              <ArrowLeft size={13} />
              Back to list
            </Button>
          }
        />

        <Card className="mb-6">
          <div className="flex items-start gap-3">
            <GitPullRequest size={16} className="text-text-dim mt-0.5 shrink-0" />
            <div>
              <div className="text-[14.5px] font-semibold text-text-bright">
                #{selectedPR.number} {selectedPR.title}
              </div>
              <div className="text-[12px] text-text-dim mt-1 flex items-center gap-2 flex-wrap">
                <span>@{selectedPR.user?.login}</span>
                <span className="text-text-dim/30">·</span>
                <span>{formatAge(selectedPR.updated_at ?? "")}</span>
                {selectedPR.draft && <StatusBadge label="DRAFT" />}
                {selectedPR.changed_files != null && (
                  <>
                    <span className="text-text-dim/30">·</span>
                    <span>{selectedPR.changed_files} file{selectedPR.changed_files !== 1 ? "s" : ""} changed</span>
                  </>
                )}
                {selectedPR.additions != null && (
                  <span className="text-success">+{selectedPR.additions}</span>
                )}
                {selectedPR.deletions != null && (
                  <span className="text-error">−{selectedPR.deletions}</span>
                )}
              </div>
            </div>
          </div>
        </Card>

        {analysing && (
          <div className="text-[12.5px] text-text-dim animate-pulse mb-6">
            Running {DIMENSIONS.length}-dimension semantic analysis…
          </div>
        )}

        {analysis && (
          <div className="space-y-5 animate-fade-up">
            <Card>
              <RiskScoreBar score={analysis.riskScore} />
            </Card>
            {Object.entries(analysis.dimensions).map(([label, res]) => (
              <Card key={label}>
                <div className="text-[13px] font-semibold text-text-bright mb-3">{label}</div>
                {res.answer && (
                  <div className="mb-4">
                    <Markdown>{res.answer}</Markdown>
                  </div>
                )}
                {res.sources && res.sources.length > 0 && (
                  <SimilarityBar sources={res.sources} title="Files consulted" />
                )}
                {!res.ok && (
                  <div className="text-[12px] text-error">Analysis failed for this dimension.</div>
                )}
              </Card>
            ))}
          </div>
        )}
      </>
    );
  }

  // ── PR list view ──────────────────────────────────────────────
  return (
    <>
      <PageHeader
        title="PR Review"
        subtitle="Semantic risk analysis on open pull requests — token stays server-side"
      />

      {/* Token status banner */}
      {tokenStatus && (
        <div className={`flex items-start gap-2.5 px-4 py-3 rounded-xl border text-[12px] mb-6 ${
          tokenStatus.has_token
            ? "bg-success/[0.05] border-success/[0.15] text-success"
            : "bg-warning/[0.05] border-warning/[0.15] text-warning"
        }`}>
          {tokenStatus.has_token
            ? <ShieldCheck size={14} className="mt-0.5 shrink-0" />
            : <ShieldAlert size={14} className="mt-0.5 shrink-0" />
          }
          <span>{tokenStatus.note}</span>
        </div>
      )}

      {/* Config */}
      <Card className="mb-7">
        <div className="grid md:grid-cols-3 gap-3 items-end mb-3">
          <div className="md:col-span-2">
            <label className="block text-[11px] font-medium text-text-dim mb-1.5">Repository</label>
            <input
              type="text"
              placeholder="owner/repo  (e.g. vercel/next.js)"
              value={ownerRepo}
              onChange={(e) => setOwnerRepo(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && fetchPRs()}
              className={INPUT_CLS}
            />
          </div>
          <div className="flex items-center gap-3">
            <Button type="button" onClick={fetchPRs} disabled={loading || !ownerRepo.trim()}>
              {loading ? "Fetching…" : "Fetch PRs"}
            </Button>
            <div className="flex items-center gap-2 ml-auto">
              <span className="text-[11px] text-text-dim shrink-0">k={topK}</span>
              <input
                type="range" min={2} max={8} value={topK}
                onChange={(e) => setTopK(Number(e.target.value))}
                aria-label="Number of sources"
                className="w-16 accent-accent"
              />
            </div>
          </div>
        </div>

        {/* Optional per-request PAT */}
        <div className="pt-3 border-t border-white/[0.05]">
          <label className="block text-[11px] font-medium text-text-dim mb-1.5">
            GitHub Personal Access Token
            <span className="ml-1.5 text-text-dim/50 font-normal">— optional, overrides server token, never stored</span>
          </label>
          <div className="relative max-w-sm">
            <input
              type={showToken ? "text" : "password"}
              placeholder="ghp_…"
              value={githubToken}
              onChange={(e) => setGithubToken(e.target.value)}
              autoComplete="off"
              className={`${INPUT_CLS} pr-10`}
            />
            <button
              type="button"
              onClick={() => setShowToken(!showToken)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-text-dim hover:text-text transition-colors"
              aria-label={showToken ? "Hide token" : "Show token"}
            >
              {showToken ? <EyeOff size={13} /> : <Eye size={13} />}
            </button>
          </div>
        </div>
      </Card>

      {error && (
        <div className="mb-6 px-4 py-3 bg-error/[0.08] border border-error/[0.18] rounded-xl text-[12.5px] text-error">
          {error}
        </div>
      )}

      {prs.length > 0 ? (
        <div className="space-y-2.5">
          <SectionTitle>{prs.length} open PR{prs.length > 1 ? "s" : ""} — {ownerRepo}</SectionTitle>
          {prs.map((pr) => (
            <Card key={pr.number} className="group">
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap mb-1">
                    <span className="text-[13.5px] font-semibold text-text-bright">
                      #{pr.number} {pr.title}
                    </span>
                    {pr.draft && <StatusBadge label="Draft" />}
                  </div>
                  <div className="text-[11.5px] text-text-dim flex items-center gap-1.5 flex-wrap">
                    <span>@{pr.user?.login}</span>
                    <span className="text-text-dim/30">·</span>
                    <span>{formatAge(pr.updated_at ?? "")}</span>
                    {pr.changed_files != null && (
                      <>
                        <span className="text-text-dim/30">·</span>
                        <span>{pr.changed_files} file{pr.changed_files !== 1 ? "s" : ""}</span>
                      </>
                    )}
                    {pr.additions != null && <span className="text-success">+{pr.additions}</span>}
                    {pr.deletions != null && <span className="text-error">−{pr.deletions}</span>}
                  </div>
                  {pr.labels && pr.labels.length > 0 && (
                    <div className="flex gap-1.5 mt-2 flex-wrap">
                      {pr.labels.map((l) => (
                        <span
                          key={l.name}
                          className="px-2 py-0.5 rounded-md text-[10px] font-medium bg-white/[0.05] border border-white/[0.09] text-text-muted"
                        >
                          {l.name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <Button type="button" size="sm" onClick={() => analyse(pr)} disabled={analysing}>
                  Analyse
                </Button>
              </div>
            </Card>
          ))}
        </div>
      ) : !loading && !error ? (
        <Card>
          <EmptyState
            title="No PRs loaded"
            description="Enter an owner/repo above and click Fetch PRs. Public repos work without a token."
          />
        </Card>
      ) : null}
    </>
  );
}
