"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { Activity, Network, RefreshCw } from "lucide-react";
import { BreadthGraphCanvas } from "@/components/breadth-flow";
import { Button, Card, EmptyState, Metric, PageHeader } from "@/components/ui";
import { getBreadthGraph, getRepositories, type BreadthFlowGraph, type Repository } from "@/lib/api";

export default function GraphPage() {
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [selectedRepoId, setSelectedRepoId] = useState("");
  const [graph, setGraph] = useState<BreadthFlowGraph | null>(null);
  const [loadingRepositories, setLoadingRepositories] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    getRepositories().then((result) => {
      if (!current) return;
      if (!result.ok) {
        setError("Could not load repositories.");
      } else {
        setRepositories(result.repos);
        setSelectedRepoId((selected) => selected || result.repos[0]?.id || "");
      }
      setLoadingRepositories(false);
    });
    return () => { current = false; };
  }, []);

  async function refreshGraph(repoId = selectedRepoId) {
    if (!repoId) return;
    setLoading(true);
    setError(null);
    setGraph(null);
    const result = await getBreadthGraph(repoId);
    if (!result.ok) {
      setError(result.error ?? "No detailed Breadth graph has been ingested for this repository.");
    } else {
      setGraph(result.flowgraph);
    }
    setLoading(false);
  }

  useEffect(() => {
    if (selectedRepoId) void refreshGraph(selectedRepoId);
  }, [selectedRepoId]);

  const selectedRepository = repositories.find((repo) => repo.id === selectedRepoId);
  const nodeCount = graph?.nodes.length ?? 0;
  const edgeCount = graph?.edges.length ?? 0;
  const insightCount = graph?.insights?.length ?? 0;

  return (
    <>
      <PageHeader
        title="Breadth graph"
        subtitle={selectedRepository ? `${selectedRepository.name} · detailed code and infrastructure flowgraph` : "Detailed code and infrastructure flowgraph"}
        action={
          <div className="flex items-center gap-2">
            <label className="sr-only" htmlFor="breadth-repo">Repository</label>
            <select
              id="breadth-repo"
              value={selectedRepoId}
              onChange={(event) => setSelectedRepoId(event.target.value)}
              disabled={loadingRepositories || repositories.length === 0}
              className="max-w-[220px] rounded-md border border-white/[0.09] bg-bg-card px-3 py-2 text-[11px] text-text"
            >
              {loadingRepositories && <option value="">Loading repositories…</option>}
              {!loadingRepositories && repositories.length === 0 && <option value="">No repositories</option>}
              {repositories.map((repo) => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
            </select>
            <Button
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => void refreshGraph()}
              disabled={loading || !selectedRepoId}
              aria-label="Refresh Breadth graph"
              title="Refresh graph"
            >
              <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
              Refresh
            </Button>
          </div>
        }
      />

      {error ? (
        <Card>
          <EmptyState
            title="Graph unavailable"
            description={error}
            action={
              <Button type="button" onClick={() => void refreshGraph()} disabled={loading || !selectedRepoId}>
                <RefreshCw size={14} />
                Try again
              </Button>
            }
          />
        </Card>
      ) : loadingRepositories || loading ? (
        <div className="flex min-h-56 items-center justify-center gap-3 rounded-lg border border-white/[0.08] bg-bg-card text-sm text-text-muted">
          <Activity size={16} className="animate-pulse text-accent" />
          Loading Breadth graph
        </div>
      ) : repositories.length === 0 ? (
        <Card>
          <EmptyState
            title="No repository indexed"
            description="Ingest a repository to build its detailed Breadth flowgraph."
            action={
              <Link href="/settings" className="inline-flex items-center gap-2 rounded-md bg-accent px-4 py-2 text-xs font-bold text-[#192110] transition hover:brightness-105">
                <Network size={14} />
                Add a repository
              </Link>
            }
          />
        </Card>
      ) : graph && (
        <>
          <div className="mb-5 grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Metric label="Breadth nodes" value={nodeCount.toLocaleString()} icon={<Network size={13} />} />
            <Metric label="Flow edges" value={edgeCount.toLocaleString()} icon={<Activity size={13} />} />
            <Metric label="Findings" value={insightCount.toLocaleString()} icon={<RefreshCw size={13} />} />
          </div>
          <BreadthGraphCanvas graph={graph} />
        </>
      )}
    </>
  );
}
