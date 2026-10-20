"use client";

import { useEffect, useState } from "react";
import { PageHeader, Metric, Card, Button, EmptyState, SectionTitle } from "@/components/ui";
import { SimilarityBar, RepoMetricsBar } from "@/components/charts";
import {
  getHealth,
  getIngestStatus,
  getLogStats,
  getSystemHealth,
  type SystemHealth,
  analyzeMockScenario,
  type MockAnalysis,
  analyzeLiveEvidence,
  type LiveAnalysis,
  getRepositories,
  getRepositorySummaries,
  getDependencyGraph,
  getBreadthGraph,
  queryCodebase,
  type BreadthFlowGraph,
  type Repository,
  type RepositoryArchitectureSummary,
  type QuerySource,
} from "@/lib/api";
import {
  Search,
  Database,
  GitCommit,
  Layers,
  FileCode,
  ScrollText,
  Activity,
  ArrowRight,
  GitBranch,
  Network,
} from "lucide-react";
import { Markdown } from "@/components/markdown";

const STATE_LABELS: Record<string, string> = {
  idle:      "Ready",
  running:   "Ingesting",
  completed: "Ready",
  failed:    "Failed",
  unknown:   "–",
};

const CHIPS = [
  "Main entry points",
  "Authentication flow",
  "Error handling",
  "Database models",
  "Retry logic",
  "API endpoints",
  "Cross-service calls",
];

interface GraphNode { id: string; name: string; team?: string; chunk_count: number; commit_count: number }
interface GraphEdge { id: string; source: string; target: string; source_name: string; target_name: string; dependency_type: string; confidence: number }

export default function DashboardPage() {
  const [health, setHealth]         = useState<Record<string, unknown>>({});
  const [stats, setStats]           = useState<Record<string, number>>({});
  const [state, setState]           = useState("unknown");
  const [repos, setRepos]           = useState<Repository[]>([]);
  const [repoSummaries, setRepoSummaries] = useState<RepositoryArchitectureSummary[]>([]);
  const [breadthGraph, setBreadthGraph] = useState<BreadthFlowGraph | null>(null);
  const [logStats, setLogStats]     = useState<Record<string, unknown>>({});
  const [systemHealth, setSystemHealth] = useState<SystemHealth | null>(null);
  const [telemetryAvailable, setTelemetryAvailable] = useState(false);
  const [telemetryChecked, setTelemetryChecked] = useState(false);
  const [mockAnalyses, setMockAnalyses] = useState<Record<string, MockAnalysis>>({});
  const [analyzingScenario, setAnalyzingScenario] = useState<string | null>(null);
  const [analysisErrors, setAnalysisErrors] = useState<Record<string, string>>({});
  const [liveAnalyses, setLiveAnalyses] = useState<Record<string, LiveAnalysis>>({});
  const [analyzingLiveService, setAnalyzingLiveService] = useState<string | null>(null);
  const [liveAnalysisErrors, setLiveAnalysisErrors] = useState<Record<string, string>>({});
  const [graphNodes, setGraphNodes] = useState<GraphNode[]>([]);
  const [graphEdges, setGraphEdges] = useState<GraphEdge[]>([]);
  const [question, setQuestion]     = useState("");
  const [useLlm, setUseLlm]         = useState(true);
  const [searching, setSearching]   = useState(false);
  const [result, setResult]         = useState<{ answer?: string; sources?: QuerySource[] } | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => {});
    getIngestStatus().then((s) => {
      setStats((s.stats as Record<string, number>) ?? {});
      setState((s.state as string) ?? "unknown");
    }).catch(() => {});
    getLogStats().then(setLogStats).catch(() => {});
    getRepositories().then((r) => {
      setRepos(r.repos);
      if (r.repos[0]) {
        getRepositorySummaries().then((summaries) => setRepoSummaries(summaries.data)).catch(() => {});
        getBreadthGraph(r.repos[0].id).then((result) => {
          if (result.ok) setBreadthGraph(result.flowgraph);
        }).catch(() => {});
      }
    }).catch(() => {});
    getDependencyGraph().then((g) => {
      if (g.ok) {
        setGraphNodes((g.nodes as GraphNode[]) ?? []);
        setGraphEdges((g.edges as GraphEdge[]) ?? []);
      }
    }).catch(() => {});
  }, []);

  useEffect(() => {
    let active = true;
    async function refresh() {
      const response = await getSystemHealth();
      if (!active) return;
      setTelemetryAvailable(response.ok);
      setTelemetryChecked(true);
      if (response.ok) setSystemHealth(response);
    }
    void refresh();
    const interval = setInterval(() => { void refresh(); }, 30_000);
    return () => { active = false; clearInterval(interval); };
  }, []);

  async function handleSearch() {
    if (!question.trim()) return;
    setSearching(true);
    const r = await queryCodebase({ question: question.trim(), top_k: 5, use_llm: useLlm });
    setResult(r);
    setSearching(false);
  }

  async function handleMockAnalysis(requestId: string, eventIds: string[]) {
    setAnalyzingScenario(requestId);
    setAnalysisErrors((errors) => ({ ...errors, [requestId]: "" }));
    const response = await analyzeMockScenario(eventIds.slice(0, 20));
    if (response.ok) {
      setMockAnalyses((analyses) => ({ ...analyses, [requestId]: response }));
    } else {
      setAnalysisErrors((errors) => ({ ...errors, [requestId]: response.error ?? "Analysis unavailable" }));
    }
    setAnalyzingScenario(null);
  }

  async function handleLiveAnalysis(serviceName: string) {
    const eventIds = systemHealth?.evidence.filter((event) => !event.synthetic && event.service === serviceName).map((event) => event.id) ?? [];
    if (eventIds.length === 0) return;
    setAnalyzingLiveService(serviceName);
    setLiveAnalysisErrors((errors) => ({ ...errors, [serviceName]: "" }));
    const response = await analyzeLiveEvidence(eventIds.slice(0, 20), primaryRepo?.id);
    if (response.ok) {
      setLiveAnalyses((analyses) => ({ ...analyses, [serviceName]: response }));
    } else {
      setLiveAnalysisErrors((errors) => ({ ...errors, [serviceName]: response.error ?? "Live analysis unavailable" }));
    }
    setAnalyzingLiveService(null);
  }

  const repoCount  = repos.length;
  const stateLabel = STATE_LABELS[state] || state;
  const isBlank    = repoCount === 0 && state !== "running";
  const primaryRepo = repos[0];
  const savedSummary = repoSummaries.find((summary) => summary.repo_id === primaryRepo?.id)?.summary;
  const breadthKinds = breadthGraph?.nodes.reduce<Record<string, number>>((counts, node) => {
    counts[node.kind] = (counts[node.kind] ?? 0) + 1;
    return counts;
  }, {}) ?? {};
  const breadthKindText = Object.entries(breadthKinds)
    .filter(([kind]) => ["service", "datastore", "queue", "topic", "external_api", "function", "module"].includes(kind))
    .sort((left, right) => right[1] - left[1])
    .slice(0, 4)
    .map(([kind, count]) => `${count.toLocaleString()} ${kind.replaceAll("_", " ")}${count === 1 ? "" : "s"}`)
    .join(", ");
  const plainBriefing = savedSummary || (repoCount === 0
    ? "No repositories are indexed yet. Connect a codebase to start building RootOps’ system map."
    : `RootOps has indexed ${repoCount} ${repoCount === 1 ? "repository" : "repositories"}, ${Number(stats.commits_ingested ?? 0).toLocaleString()} commits, and ${Number(stats.chunks_ingested ?? 0).toLocaleString()} code chunks. ${breadthGraph ? `Breadth mapped ${breadthGraph.nodes.length.toLocaleString()} nodes and ${breadthGraph.edges.length.toLocaleString()} connections${breadthKindText ? `, including ${breadthKindText}` : ""}.` : "A detailed Breadth map is not available for the selected repository yet."} ${logStats.ok ? `${Number(logStats.total_entries ?? 0).toLocaleString()} log events are indexed.` : "Telemetry status is unavailable."}`);

  return (
    <>
      <PageHeader
        title="System overview"
        subtitle={`${repoCount} ${repoCount === 1 ? "repository" : "repositories"} indexed · ${stateLabel}`}
        action={
          <div className="flex items-center gap-2 px-3 py-2 rounded-md border border-white/[0.08] bg-bg-card text-xs text-text-muted">
            <span
              className={`w-2 h-2 rounded-full ${
                health.ok
                  ? "bg-success shadow-[0_0_6px_rgba(16,185,129,0.5)]"
                  : "bg-error shadow-[0_0_6px_rgba(239,68,68,0.5)]"
              }`}
            />
            {health.ok ? "API connected" : "API offline"}
          </div>
        }
      />

      {/* ── Onboarding wizard (shown only on fresh install) ─────── */}
      {isBlank && (
        <section className="mb-9 border-y border-white/[0.08] py-5">
          <div className="flex flex-wrap items-end justify-between gap-3 mb-5">
            <div>
              <div className="text-[15px] font-semibold text-text-bright mb-1">
                Your workspace is ready
              </div>
              <p className="text-[12px] text-text-muted leading-relaxed">
                Connect a repository to begin indexing your system.
              </p>
            </div>
            <a href="/settings" className="inline-flex items-center gap-2 text-[12px] font-semibold text-accent hover:text-text-bright transition-colors">
              Add repository <ArrowRight size={14} />
            </a>
          </div>
          <div className="grid md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-white/[0.08]">
            {[
              {
                step: "1",
                title: "Connect a Repository",
                desc: "Go to Settings and paste a GitHub URL or local path.",
                href: "/settings",
                cta: "Go to Settings →",
              },
              {
                step: "2",
                title: "Ingest & Embed",
                desc: "RootOps chunks your code and builds a semantic vector index.",
                href: "/settings",
                cta: "Start Ingestion →",
              },
              {
                step: "3",
                title: "Ask Questions",
                desc: "Query your codebase in natural language or stream AI answers.",
                href: "/intelligence",
                cta: "Open Intelligence →",
              },
            ].map(({ step, title, desc, href, cta }) => (
              <div key={step} className="flex flex-col items-start gap-3 py-4 md:px-5 first:md:pl-0 last:md:pr-0">
                <div className="w-7 h-7 rounded-md bg-accent text-[#192110] flex items-center justify-center text-[12px] font-bold">
                  {step}
                </div>
                <div>
                  <div className="text-[13px] font-semibold text-text-bright mb-1">{title}</div>
                  <p className="text-[12px] text-text-dim leading-relaxed">{desc}</p>
                </div>
                <a
                  href={href}
                  className="mt-auto inline-flex items-center gap-1 text-[11px] font-semibold text-text-muted hover:text-accent transition-colors"
                >
                  {cta} <ArrowRight size={12} />
                </a>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ── Metrics ──────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-3 mb-10">
        <Metric label="Repositories" value={repoCount}
          icon={<Database size={12} />} />
        <Metric label="Commits"      value={(stats.commits_ingested ?? 0).toLocaleString()}
          icon={<GitCommit size={12} />} />
        <Metric label="Chunks"       value={(stats.chunks_ingested ?? 0).toLocaleString()}
          icon={<Layers size={12} />} />
        <Metric label="Files indexed" value={(stats.files_indexed ?? stats.files_processed ?? 0).toLocaleString()}
          icon={<FileCode size={12} />} />
        <Metric
          label="Logs"
          value={logStats.ok ? ((logStats.total_entries as number) ?? 0).toLocaleString() : "–"}
          icon={<ScrollText size={12} />}
        />
        <Metric label="Status" value={stateLabel}
          icon={<Activity size={12} />}
          accent={state === "running"}
        />
      </div>

      <section className="mb-10 grid gap-6 border-y border-white/[0.08] py-5 lg:grid-cols-[minmax(0,1fr)_300px] lg:items-center">
        <div>
          <SectionTitle>System briefing</SectionTitle>
          <h2 className="mb-2 text-[17px] font-semibold text-text-bright">
            {primaryRepo ? primaryRepo.name : "What RootOps knows"}
          </h2>
          <p className="max-w-[76ch] text-[12px] leading-relaxed text-text-muted">
            {plainBriefing}
          </p>
          {primaryRepo?.last_ingested_at && (
            <p className="mt-2 text-[10px] text-text-dim">
              Last indexed {new Date(primaryRepo.last_ingested_at).toLocaleString()}
            </p>
          )}
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3 lg:grid-cols-1">
          <a href="/settings" className="flex items-center justify-between gap-3 border-b border-white/[0.07] py-2 text-[11px] hover:text-accent">
            <span className="inline-flex items-center gap-2 text-text-muted"><Database size={14} /> Codebases</span>
            <span className="font-mono text-text-bright">{repoCount}</span>
          </a>
          <a href="/graph" className="flex items-center justify-between gap-3 border-b border-white/[0.07] py-2 text-[11px] hover:text-accent">
            <span className="inline-flex items-center gap-2 text-text-muted"><Network size={14} /> Breadth map</span>
            <span className="font-mono text-text-bright">{breadthGraph ? `${breadthGraph.nodes.length.toLocaleString()} / ${breadthGraph.edges.length.toLocaleString()}` : "Not scanned"}</span>
          </a>
          <a href="/logs" className="flex items-center justify-between gap-3 border-b border-white/[0.07] py-2 text-[11px] hover:text-accent">
            <span className="inline-flex items-center gap-2 text-text-muted"><ScrollText size={14} /> Telemetry</span>
            <span className="font-mono text-text-bright">{logStats.ok ? Number(logStats.total_entries ?? 0).toLocaleString() : "Unavailable"}</span>
          </a>
        </div>
      </section>

      <section aria-label="System health" className="mb-10 border-b border-white/[0.08] pb-5">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <SectionTitle>System health</SectionTitle>
            <h2 className="text-[17px] font-semibold text-text-bright">
              {!systemHealth ? telemetryChecked ? "Telemetry unavailable" : "Checking telemetry" : !telemetryAvailable ? "Telemetry unavailable" : systemHealth.status === "errors_observed" ? "Errors observed" : "Insufficient live evidence"}
            </h2>
            <p className="mt-2 max-w-[76ch] text-[12px] leading-relaxed text-text-muted">
              {!systemHealth ? "Recent telemetry has not loaded." : !telemetryAvailable ? "The latest telemetry check failed. Evidence below is from the last successful check." : systemHealth.status === "errors_observed" ? "Recent non-synthetic error logs need investigation. Logs alone do not establish availability or root cause." : "No real errors are visible in this sample. This does not establish that the system is healthy; mock events are excluded from health assessment."}
            </p>
          </div>
          <a href="/logs" className="inline-flex items-center gap-2 text-[12px] text-accent">Log evidence <ArrowRight size={14} /></a>
        </div>
        {systemHealth && (
          <>
            <p className="mb-3 text-[11px] text-text-dim">
              Last {systemHealth.window_minutes} minutes · {systemHealth.sampled_events} retained events{systemHealth.truncated ? " · Latest 500 only; sample incomplete" : ""} · Checked {new Date(systemHealth.checked_at).toLocaleTimeString()}
            </p>
            {[false, true].map((synthetic) => {
              const services = systemHealth.services.filter((service) => service.synthetic === synthetic);
              const correlations = systemHealth.correlations.filter((group) => group.synthetic === synthetic);
              const evidence = systemHealth.evidence.filter((event) => event.synthetic === synthetic);
              const EvidenceSection = synthetic ? "details" : "div";
              if (synthetic && services.length === 0 && correlations.length === 0 && evidence.length === 0) return null;
              return (
              <EvidenceSection key={synthetic ? "demo" : "live"} className={synthetic ? "mt-4 border-t border-white/[0.07] pt-3 text-[12px]" : ""}>
              {synthetic && <summary className="cursor-pointer text-text-muted">Demo evidence</summary>}
            <div className="divide-y divide-white/[0.07]">
              {services.map((service) => (
                <div key={`${service.name}:${service.synthetic}`} className="min-w-0 py-3 text-[12px]">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="min-w-0 break-all font-medium text-text-bright">{service.name} <span className="text-[10px] font-normal text-text-dim">{service.synthetic ? "SYNTHETIC" : "LIVE LOGS"}</span></span>
                  <span className="text-text-muted">{service.errors} {service.errors === 1 ? "error" : "errors"} · {service.warnings} {service.warnings === 1 ? "warning" : "warnings"} · {service.events} {service.events === 1 ? "event" : "events"}</span>
                  </div>
                  {!synthetic && (
                    <div className="mt-3 min-w-0">
                      <div className="flex flex-wrap items-center gap-3">
                        <button type="button"
                          disabled={analyzingLiveService !== null || !telemetryAvailable || !evidence.some((event) => event.service === service.name)}
                          onClick={() => { void handleLiveAnalysis(service.name); }}
                          className="inline-flex items-center gap-2 rounded-md border border-accent/30 px-3 py-2 text-accent transition-colors hover:bg-accent/10 disabled:opacity-50">
                          <Activity size={14} /> {analyzingLiveService === service.name ? "Analyzing live evidence..." : "Analyze live evidence"}
                        </button>
                        {primaryRepo && <span className="text-[10px] text-text-dim">Source context: {primaryRepo.name}</span>}
                      </div>
                      {liveAnalysisErrors[service.name] && <p role="alert" className="mt-2 break-words text-error">{liveAnalysisErrors[service.name]}</p>}
                      {liveAnalyses[service.name] && (
                        <article aria-label={`Live analysis ${service.name}`} className="mt-4 min-w-0 border-l-2 border-accent/40 pl-4">
                          <h3 className="mb-2 font-semibold text-text-bright">Live evidence analysis</h3>
                          <p className="mb-3 text-[11px] text-text-dim">{liveAnalyses[service.name].notice}</p>
                          <Markdown content={liveAnalyses[service.name].analysis} />
                          <details className="mt-4">
                            <summary className="cursor-pointer text-text-bright">Selected live evidence · {liveAnalyses[service.name].timeline.length} events</summary>
                            <ol className="mt-3 space-y-3">
                              {liveAnalyses[service.name].timeline.map((event) => (
                                <li key={event.id} className="min-w-0 break-words text-text-muted">
                                  <p>{new Date(event.timestamp).toLocaleTimeString()} · {event.level} · {event.message}</p>
                                  <p className="mt-1 break-all font-mono text-[10px] text-text-dim">{event.id}</p>
                                </li>
                              ))}
                            </ol>
                          </details>
                          {!!liveAnalyses[service.name].sources.length && (
                            <details className="mt-4">
                              <summary className="cursor-pointer text-text-bright">Source code references · {liveAnalyses[service.name].sources.length}</summary>
                              {liveAnalyses[service.name].sources.map((source) => (
                                <div key={`${source.file_path}:${source.start_line}`} className="mt-3 min-w-0">
                                  <p className="break-all font-mono text-[10px] text-text-muted">{source.file_path}:{source.start_line}-{source.end_line}</p>
                                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words text-[10px] text-text-dim"><code>{source.content}</code></pre>
                                </div>
                              ))}
                            </details>
                          )}
                        </article>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
            {services.length === 0 && <p className="py-3 text-[12px] text-text-muted">No retained live log events in this window.</p>}
            {correlations.map((group) => (
              <div key={`${group.request_id}:${group.synthetic}`} className="border-t border-white/[0.07] py-3 text-[12px]">
              <details>
                <summary className="cursor-pointer break-all text-text-bright">{group.synthetic ? "Synthetic scenario" : "Related events"}: {group.request_id} · {group.event_ids.length} events</summary>
                <p className="mt-2 text-[11px] text-text-dim">{group.basis}</p>
                <ul className="mt-2 space-y-2">
                  {evidence.filter((event) => group.event_ids.includes(event.id)).map((event) => (
                    <li key={event.id} className="break-words text-text-muted">{event.service}: {event.message}</li>
                  ))}
                </ul>
              </details>
              {group.synthetic && (
                <div className="mt-3">
                  <button type="button" disabled={analyzingScenario !== null || !telemetryAvailable}
                    onClick={() => { void handleMockAnalysis(group.request_id, group.event_ids); }}
                    className="inline-flex items-center gap-2 rounded-md border border-accent/30 px-3 py-2 text-accent transition-colors hover:bg-accent/10 disabled:cursor-wait disabled:opacity-50">
                    <Activity size={14} /> {analyzingScenario === group.request_id ? "Analyzing mock evidence..." : "Analyze scenario"}
                  </button>
                  {analysisErrors[group.request_id] && <p role="alert" className="mt-2 break-words text-error">{analysisErrors[group.request_id]}</p>}
                  {mockAnalyses[group.request_id] && (
                    <article aria-label={`Mock analysis ${group.request_id}`} className="mt-4 min-w-0 border-l-2 border-accent/40 pl-4">
                      <h3 className="mb-2 font-semibold text-text-bright">Synthetic incident analysis</h3>
                      <p className="mb-3 text-[11px] text-text-dim">{mockAnalyses[group.request_id].notice}</p>
                      <Markdown content={mockAnalyses[group.request_id].analysis} />
                      <h4 className="mb-3 mt-5 font-semibold text-text-bright">Evidence timeline · {Math.min(group.event_ids.length, 20)} selected events</h4>
                      <ol className="space-y-4">
                        {mockAnalyses[group.request_id].timeline.map((event) => (
                          <li key={event.id} className="min-w-0 break-words">
                            <p className="text-text-bright">{new Date(event.timestamp).toLocaleTimeString()} · {event.service} · {event.level}</p>
                            <p className="mt-1 text-text-muted">{event.message}</p>
                            <p className="mt-1 break-all font-mono text-[10px] text-text-dim">{event.id}</p>
                          </li>
                        ))}
                      </ol>
                    </article>
                  )}
                </div>
              )}
              </div>
            ))}
            <details className="border-t border-white/[0.07] pt-3 text-[12px]">
              <summary className="cursor-pointer text-text-bright">{synthetic ? "Recent demo evidence" : "Recent live evidence"}</summary>
              <ul className="mt-3 divide-y divide-white/[0.07]">
                {evidence.map((event) => (
                  <li key={event.id} className="py-3">
                    <div className="break-all text-[10px] text-text-dim">{event.synthetic ? "SYNTHETIC · " : ""}{event.service} · {event.level ?? "UNKNOWN"} · {new Date(event.timestamp).toLocaleTimeString()}</div>
                    <p className="mt-1 break-words text-text-muted">{event.message}</p>
                  </li>
                ))}
              </ul>
            </details>
              </EvidenceSection>
              );
            })}
          </>
        )}
      </section>

      {/* ── Two-column layout: repos + quick search ──────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-[1.1fr_0.9fr] gap-8 xl:gap-12 mb-10">
        {/* Left: Repos */}
        <div>
          {repos.length > 0 ? (
            <>
              <SectionTitle>Repositories</SectionTitle>
              <RepoMetricsBar repos={repos} />
            </>
          ) : (
            <Card>
              <EmptyState
                title="No repositories yet"
                description="Connect your first repository to start analysing code."
                action={
                  <a
                    href="/settings"
                    className="inline-flex items-center gap-1.5 px-4 py-2 rounded-xl text-[12.5px] font-semibold bg-white/[0.05] border border-white/10 text-text hover:bg-white/[0.08] hover:border-white/[0.16] transition-all"
                  >
                    Go to Settings →
                  </a>
                }
              />
            </Card>
          )}
        </div>

        {/* Right: Quick Search */}
        <div>
          <SectionTitle>Quick Search</SectionTitle>
          <p className="text-[12px] text-text-dim mb-4">
            Ask anything about your codebase — full streaming conversations in{" "}
            <a href="/intelligence" className="text-accent/80 hover:text-accent transition-colors">
              Intelligence
            </a>
          </p>

          {/* Chips */}
          <div className="flex flex-wrap gap-1.5 mb-4">
            {CHIPS.map((chip) => (
              <button
                key={chip}
                onClick={() => setQuestion(chip)}
                className="px-3 py-1.5 text-[11.5px] text-text-muted bg-white/[0.035] border border-white/[0.08] rounded-full hover:bg-accent-dim hover:border-accent/25 hover:text-accent transition-all duration-150"
              >
                {chip}
              </button>
            ))}
          </div>

          <div className="flex gap-2.5 mb-4">
            <div className="relative flex-1">
              <Search size={14} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-text-dim pointer-events-none" />
              <input
                type="text"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                placeholder="What does the payment processor do?"
                className="w-full pl-9 pr-4 py-2.5 bg-bg-input border border-white/[0.09] rounded-xl text-[13px] text-text placeholder:text-text-dim focus:border-accent/40 focus:ring-2 focus:ring-accent/[0.07] outline-none transition-all"
              />
            </div>
            <label className="flex items-center gap-2 text-[11.5px] text-text-muted cursor-pointer select-none shrink-0">
              <input
                type="checkbox"
                checked={useLlm}
                onChange={(e) => setUseLlm(e.target.checked)}
                className="accent-accent"
              />
              LLM
            </label>
          </div>

          <Button type="button" onClick={handleSearch} disabled={!question.trim() || searching}>
            <Search size={14} />
            {searching ? "Searching…" : "Search"}
          </Button>

          {result && (
            <div className="mt-6 space-y-4 animate-fade-up">
              {result.answer && (
                <Card className="border-accent/[0.15] bg-accent/[0.025]">
                  <div className="text-[13px] text-text leading-relaxed break-words">
                    <Markdown content={result.answer} />
                  </div>
                </Card>
              )}
              {result.sources && result.sources.length > 0 && (
                <SimilarityBar sources={result.sources} title="Source files" />
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Dependency Graph ─────────────────────────────────────── */}
      {graphNodes.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <SectionTitle>
              <span className="inline-flex items-center gap-1.5">
                <Network size={12} />
                Service Dependency Graph · {graphNodes.length} node{graphNodes.length !== 1 ? "s" : ""}
                {graphEdges.length > 0 && `, ${graphEdges.length} edge${graphEdges.length !== 1 ? "s" : ""}`}
              </span>
            </SectionTitle>
            <a href="/graph" className="mb-3 inline-flex items-center gap-1.5 text-[11px] font-semibold text-accent hover:text-text-bright transition-colors">
              Open graph <ArrowRight size={13} />
            </a>
          </div>

          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-3">
            {graphNodes.map((node) => {
              const outbound = graphEdges.filter((e) => e.source === node.id);
              const inbound  = graphEdges.filter((e) => e.target === node.id);

              return (
                <a
                  key={node.id}
                  href="/graph"
                  className="block rounded-[14px] border border-white/[0.07] bg-[rgba(8,8,12,0.8)] p-4 hover:border-accent/30 hover:bg-accent/[0.02] transition-all duration-150 group"
                >
                  <div className="flex items-start gap-3 mb-3">
                    <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-accent/20 to-info/20 border border-white/[0.08] flex items-center justify-center shrink-0">
                      <GitBranch size={13} className="text-accent/70" />
                    </div>
                    <div className="min-w-0">
                      <div className="text-[13px] font-semibold text-text-bright group-hover:text-accent transition-colors truncate">
                        {node.name}
                      </div>
                      {node.team && (
                        <div className="text-[10.5px] text-text-dim mt-0.5">{node.team}</div>
                      )}
                    </div>
                  </div>

                  <div className="flex gap-3 text-[11px] text-text-dim mb-3">
                    <span>{node.chunk_count.toLocaleString()} chunks</span>
                    <span className="text-text-dim/30">·</span>
                    <span>{node.commit_count.toLocaleString()} commits</span>
                  </div>

                  {(outbound.length > 0 || inbound.length > 0) && (
                    <div className="space-y-1 pt-3 border-t border-white/[0.05]">
                      {outbound.slice(0, 3).map((e) => (
                        <div key={e.id} className="flex items-center gap-1.5 text-[11px]">
                          <span className="text-accent/50">→</span>
                          <span className="text-text-dim truncate">{e.target_name}</span>
                          <span className="ml-auto text-text-dim/50 shrink-0">{e.dependency_type}</span>
                        </div>
                      ))}
                      {inbound.slice(0, 2).map((e) => (
                        <div key={e.id} className="flex items-center gap-1.5 text-[11px]">
                          <span className="text-info/50">←</span>
                          <span className="text-text-dim truncate">{e.source_name}</span>
                        </div>
                      ))}
                      {(outbound.length + inbound.length) > 5 && (
                        <div className="text-[10.5px] text-text-dim/50">
                          +{outbound.length + inbound.length - 5} more
                        </div>
                      )}
                    </div>
                  )}
                </a>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}
