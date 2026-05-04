"use client";

import { useEffect, useState } from "react";
import { PageHeader, Card, Button, EmptyState, SectionTitle } from "@/components/ui";
import { Markdown } from "@/lib/markdown";
import {
  getHealth,
  getLogStats,
  runHeal,
  getFixes,
  createHealPr,
  type PendingFix,
} from "@/lib/api";
import { Zap, ChevronDown, ExternalLink } from "lucide-react";

const INPUT_CLS =
  "w-full px-3.5 py-2.5 bg-bg-input border border-white/[0.09] rounded-xl text-[13px] text-text placeholder:text-text-dim focus:border-accent/40 focus:ring-2 focus:ring-accent/[0.07] outline-none transition-all";

export default function AutoHealPage() {
  const [logStats, setLogStats]     = useState<Record<string, unknown>>({});
  const [fixes, setFixes]           = useState<PendingFix[]>([]);
  const [diagnosing, setDiagnosing] = useState(false);
  const [diagResult, setDiagResult] = useState<string | null>(null);
  const [selectedService, setSelectedService] = useState("All services");
  const [prResults, setPrResults]   = useState<Record<string, { ok: boolean; pr_number?: number; pr_url?: string; error?: string }>>({});
  const [prRepos, setPrRepos]       = useState<Record<string, string>>({});
  const [prBranches, setPrBranches] = useState<Record<string, string>>({});
  const [expandedFix, setExpandedFix] = useState<string | null>(null);

  function refresh() {
    getHealth().catch(() => {});
    getLogStats().then(setLogStats);
    getFixes().then((r) => setFixes(r.fixes));
  }

  useEffect(refresh, []);

  const byService     = (logStats.by_service as Record<string, number>) ?? {};
  const serviceOptions = ["All services", ...Object.keys(byService).sort()];
  const logCount      = (logStats.total_entries as number) ?? 0;
  const hasLogs       = logStats.ok && logCount > 0;

  async function handleDiagnose() {
    setDiagnosing(true);
    setDiagResult(null);
    try {
      const svc = selectedService === "All services" ? undefined : selectedService;
      const res = await runHeal(svc);
      setDiagResult(
        res.ok
          ? `${(res.diagnoses_count as number) ?? 0} fix(es) generated.`
          : res.error || "Failed",
      );
      refresh();
    } catch (err) {
      setDiagResult(err instanceof Error ? err.message : "Unexpected error");
    } finally {
      setDiagnosing(false);
    }
  }

  async function handleCreatePR(fix: PendingFix) {
    const repo   = prRepos[fix.fix_id]    || "";
    const branch = prBranches[fix.fix_id] || "main";
    if (!repo || !fix.suggested_code) return;
    try {
      const res = await createHealPr(fix.fix_id, repo, fix.suggested_code, branch);
      setPrResults({
        ...prResults,
        [fix.fix_id]: {
          ok:        !!res.ok,
          pr_number: res.pr_number as number | undefined,
          pr_url:    res.pr_url as string | undefined,
          error:     res.error,
        },
      });
    } catch (err) {
      setPrResults({
        ...prResults,
        [fix.fix_id]: { ok: false, error: err instanceof Error ? err.message : "Unexpected error" },
      });
    }
  }

  return (
    <>
      <PageHeader
        title="Auto-Heal"
        subtitle="Scan error logs, find relevant code, generate LLM-powered fixes, open PRs"
      />

      {/* Diagnosis card */}
      <Card className="mb-8">
        <div className="text-[14px] font-semibold text-text-bright mb-4">Run Diagnosis</div>

        {!hasLogs && (
          <div className="mb-4 px-3.5 py-2.5 bg-warning/[0.07] border border-warning/[0.18] rounded-xl text-[12.5px] text-warning">
            No logs ingested yet. Go to{" "}
            <a href="/logs" className="underline underline-offset-2">Log Ingest</a> first.
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <select
            value={selectedService}
            onChange={(e) => setSelectedService(e.target.value)}
            aria-label="Select service"
            className="px-3.5 py-2.5 bg-bg-input border border-white/[0.09] rounded-xl text-[13px] text-text focus:border-accent/40 outline-none transition-all min-w-[160px]"
          >
            {serviceOptions.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>

          <Button type="button" onClick={handleDiagnose} disabled={diagnosing || !hasLogs}>
            <Zap size={13} />
            {diagnosing ? "Running…" : "Run Diagnosis"}
          </Button>

          {diagResult && (
            <span className={`text-[12.5px] font-medium ${diagResult.includes("fix") ? "text-success" : "text-error"}`}>
              {diagResult}
            </span>
          )}
        </div>
      </Card>

      {/* Pending fixes */}
      <SectionTitle>
        Pending Fixes {fixes.length > 0 && `· ${fixes.length}`}
      </SectionTitle>

      {fixes.length === 0 ? (
        <Card>
          <EmptyState
            title="No pending fixes"
            description="Run a diagnosis above to scan logs and generate fixes."
          />
        </Card>
      ) : (
        <div className="space-y-3">
          {fixes.map((fix) => {
            const prDone = !!prResults[fix.fix_id]?.ok;
            const expanded = expandedFix === fix.fix_id;

            return (
              <Card key={fix.fix_id}>
                {/* Header */}
                <div className="flex items-start justify-between gap-4 mb-3">
                  <div className="flex-1 min-w-0">
                    <div className="text-[13.5px] font-semibold text-text-bright leading-snug mb-1">
                      {(fix.error_message || `Fix ${fix.fix_id}`).slice(0, 120)}
                    </div>
                    <div className="text-[11.5px] text-text-dim">
                      {fix.related_file}
                      {fix.similarity_score != null && ` · ${(fix.similarity_score * 100).toFixed(0)}% match`}
                    </div>
                  </div>
                  {prDone && (
                    <span className="text-[11.5px] font-semibold text-success shrink-0">✓ PR created</span>
                  )}
                </div>

                {/* Root cause */}
                {fix.diagnosis && (
                  <div className="mb-4 p-3.5 bg-white/[0.02] border border-white/[0.06] rounded-xl">
                    <div className="text-[10px] font-bold uppercase tracking-widest text-text-dim mb-2">
                      Root Cause & Fix
                    </div>
                    <Markdown>{fix.diagnosis}</Markdown>
                  </div>
                )}

                {/* Suggested code (collapsible) */}
                {fix.suggested_code && (
                  <div className="mb-4">
                    <button
                      type="button"
                      onClick={() => setExpandedFix(expanded ? null : fix.fix_id)}
                      className="flex items-center gap-1.5 text-[11.5px] text-text-dim hover:text-text transition-colors mb-2"
                    >
                      <ChevronDown size={13} className={`transition-transform duration-200 ${expanded ? "rotate-180" : ""}`} />
                      Suggested code
                    </button>
                    {expanded && (
                      <pre className="text-[11px] text-text-muted bg-[#06060A] border border-white/[0.06] rounded-xl p-4 overflow-x-auto font-mono animate-fade-up">
                        {fix.suggested_code}
                      </pre>
                    )}
                  </div>
                )}

                {/* PR creation */}
                {!prDone && (
                  <div className="flex items-center gap-2.5 pt-3 border-t border-white/[0.05]">
                    <input
                      type="text"
                      placeholder="owner/repo"
                      value={prRepos[fix.fix_id] || ""}
                      onChange={(e) => setPrRepos((p) => ({ ...p, [fix.fix_id]: e.target.value }))}
                      className={`${INPUT_CLS} flex-1`}
                    />
                    <input
                      type="text"
                      placeholder="main"
                      value={prBranches[fix.fix_id] || "main"}
                      onChange={(e) => setPrBranches((p) => ({ ...p, [fix.fix_id]: e.target.value }))}
                      className={`${INPUT_CLS} w-28`}
                    />
                    <Button
                      type="button"
                      size="sm"
                      onClick={() => handleCreatePR(fix)}
                      disabled={!prRepos[fix.fix_id] || !fix.suggested_code}
                    >
                      Create PR
                    </Button>
                  </div>
                )}

                {prResults[fix.fix_id] && !prResults[fix.fix_id].ok && (
                  <div className="mt-2 text-[12px] text-error">{prResults[fix.fix_id].error}</div>
                )}
                {prDone && prResults[fix.fix_id].pr_url && (
                  <div className="mt-3 pt-3 border-t border-white/[0.05]">
                    <a
                      href={prResults[fix.fix_id].pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-[12.5px] text-success hover:text-success/80 transition-colors"
                    >
                      <ExternalLink size={12} />
                      PR #{prResults[fix.fix_id].pr_number} opened
                    </a>
                  </div>
                )}
              </Card>
            );
          })}
        </div>
      )}
    </>
  );
}
