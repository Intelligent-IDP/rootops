"use client";

import { useState } from "react";

export interface GraphNode {
  id: string;
  name: string;
  entity_type?: string;
  repository_count?: number;
  team?: string | null;
  chunk_count: number;
  commit_count: number;
  unresolved?: boolean;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  source_name: string;
  target_name: string;
  dependency_type: string;
  call_count: number;
  confidence: number;
  promotion_level?: string;
}

const WIDTH = 1000;
const HEIGHT = 620;

function graphNodesWithUnresolvedTargets(nodes: GraphNode[], edges: GraphEdge[]) {
  const resolvedNames = new Set(nodes.map((node) => node.name.toLowerCase()));
  const unresolved = new Map<string, GraphNode>();

  for (const edge of edges) {
    for (const [id, name] of [
      [edge.source, edge.source_name],
      [edge.target, edge.target_name],
    ]) {
      if (id || resolvedNames.has(name.toLowerCase())) continue;
      const unresolvedId = `unresolved:${name.toLowerCase()}`;
      unresolved.set(unresolvedId, {
        id: unresolvedId,
        name,
        chunk_count: 0,
        commit_count: 0,
        unresolved: true,
      });
    }
  }

  return [...nodes, ...unresolved.values()];
}

function nodePositions(nodes: GraphNode[], edges: GraphEdge[]) {
  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    if (edge.target) degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }

  const ordered = [...nodes].sort(
    (left, right) => (degree.get(right.id) ?? 0) - (degree.get(left.id) ?? 0),
  );
  const centerX = WIDTH / 2;
  const centerY = HEIGHT / 2;
  const radiusX = Math.min(350, 115 + ordered.length * 12);
  const radiusY = Math.min(205, 75 + ordered.length * 7);

  return new Map(
    ordered.map((node, index) => {
      const angle = ordered.length === 1
        ? -Math.PI / 2
        : -Math.PI / 2 + (index * 2 * Math.PI) / ordered.length;
      return [node.id, {
        x: centerX + Math.cos(angle) * radiusX,
        y: centerY + Math.sin(angle) * radiusY,
      }];
    }),
  );
}

function sourceId(edge: GraphEdge) {
  return edge.source || `unresolved:${edge.source_name.toLowerCase()}`;
}

function targetId(edge: GraphEdge) {
  return edge.target || `unresolved:${edge.target_name.toLowerCase()}`;
}

function entityColor(entityType?: string) {
  switch (entityType?.toLowerCase()) {
    case "database": return "#87BDF5";
    case "queue": return "#F1C27B";
    case "topic": return "#8ECFC0";
    case "api": return "#E597B4";
    default: return "#C8F169";
  }
}

export function DependencyGraphCanvas({
  nodes,
  edges,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const allNodes = graphNodesWithUnresolvedTargets(nodes, edges);
  const positions = nodePositions(allNodes, edges);
  const selected = allNodes.find((node) => node.id === selectedId) ?? null;
  const selectedEdges = selected
    ? edges.filter((edge) => sourceId(edge) === selected.id || targetId(edge) === selected.id)
    : [];

  function selectWithKeyboard(event: React.KeyboardEvent<SVGGElement>, id: string) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelectedId(id);
    }
  }

  return (
    <div className="overflow-hidden rounded-lg border border-white/[0.09] bg-[#0D140F]">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-4 py-3">
        <div className="flex items-center gap-4 text-[11px] text-text-muted">
          <span className="inline-flex items-center gap-2">
            <span className="h-px w-5 bg-[#A7C67B]" />
            Structural
          </span>
          <span className="inline-flex items-center gap-2">
            <span className="h-0 w-5 border-t border-dashed border-[#87BDF5]" />
            Causal evidence
          </span>
          <span className="inline-flex items-center gap-2">
            <span className="h-2 w-2 rounded-full border border-text-dim bg-transparent" />
            Unregistered
          </span>
        </div>
        <span className="text-[10px] font-mono text-text-dim">DEPENDENCY TOPOLOGY</span>
      </div>

      <div className="hidden overflow-x-auto md:block">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`Dependency graph with ${nodes.length} registered entities and ${edges.length} edges`}
          className="block h-auto min-h-[360px] w-full min-w-[720px]"
        >
          <defs>
            <pattern id="graph-grid" width="28" height="28" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="1" fill="rgba(196,220,199,0.12)" />
            </pattern>
            <filter id="node-halo" x="-100%" y="-100%" width="300%" height="300%">
              <feGaussianBlur stdDeviation="8" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            <marker
              id="edge-arrow"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#A7C67B" />
            </marker>
          </defs>

          <rect width={WIDTH} height={HEIGHT} fill="url(#graph-grid)" />

          {edges.map((edge, index) => {
            const source = positions.get(sourceId(edge));
            const target = positions.get(targetId(edge));
            if (!source || !target) return null;

            const dx = target.x - source.x;
            const dy = target.y - source.y;
            const distance = Math.max(1, Math.hypot(dx, dy));
            const bend = (index % 5 - 2) * 18;
            const controlX = (source.x + target.x) / 2 - (dy / distance) * bend;
            const controlY = (source.y + target.y) / 2 + (dx / distance) * bend;
            const isRelated = selected && (sourceId(edge) === selected.id || targetId(edge) === selected.id);
            const isCausal = edge.promotion_level && edge.promotion_level !== "structural";

            return (
              <g key={edge.id}>
                <path
                  d={`M ${source.x} ${source.y} Q ${controlX} ${controlY} ${target.x} ${target.y}`}
                  fill="none"
                  stroke={isRelated ? "#C8F169" : isCausal ? "#87BDF5" : "#A7C67B"}
                  strokeWidth={isRelated ? 2 : 1.4}
                  strokeOpacity={selected && !isRelated ? 0.12 : 0.52}
                  strokeDasharray={!edge.target ? "5 6" : isCausal ? "3 6" : undefined}
                  markerEnd="url(#edge-arrow)"
                  className="transition-all duration-200"
                >
                  <title>
                    {edge.source_name} {edge.dependency_type} {edge.target_name} · {edge.call_count} call sites
                  </title>
                </path>
              </g>
            );
          })}

          {allNodes.map((node) => {
            const position = positions.get(node.id);
            if (!position) return null;
            const isSelected = selectedId === node.id;
            const isConnected = selectedEdges.some(
              (edge) => sourceId(edge) === node.id || targetId(edge) === node.id,
            );

            return (
              <g
                key={node.id}
                role="button"
                tabIndex={0}
                aria-label={`${node.name}${node.unresolved ? ", not registered" : ""}`}
                aria-pressed={isSelected}
                onClick={() => setSelectedId(node.id)}
                onKeyDown={(event) => selectWithKeyboard(event, node.id)}
                className="cursor-pointer outline-none"
                opacity={selected && !isConnected && !isSelected ? 0.42 : 1}
              >
                <circle
                  cx={position.x}
                  cy={position.y}
                  r={isSelected ? 30 : 25}
                  fill={node.unresolved ? "#131A15" : "#172118"}
                  stroke={node.unresolved ? "#75816F" : isSelected ? "#D8FF8D" : entityColor(node.entity_type)}
                  strokeWidth={isSelected ? 2 : 1.5}
                  strokeDasharray={node.unresolved ? "4 4" : undefined}
                  filter={node.unresolved ? undefined : "url(#node-halo)"}
                />
                <circle
                  cx={position.x}
                  cy={position.y}
                  r="4"
                  fill={node.unresolved ? "#788377" : entityColor(node.entity_type)}
                />
                <text
                  x={position.x}
                  y={position.y + 43}
                  textAnchor="middle"
                  fill={node.unresolved ? "#A1ACA2" : "#EAF2E8"}
                  fontSize="13"
                  fontWeight="600"
                >
                  {node.name.length > 22 ? `${node.name.slice(0, 20)}…` : node.name}
                </text>
                <text
                  x={position.x}
                  y={position.y + 60}
                  textAnchor="middle"
                  fill="#849188"
                  fontSize="10"
                  letterSpacing="1.2"
                >
                  {node.unresolved
                    ? "NOT REGISTERED"
                    : `${node.entity_type ?? "ENTITY"}${node.repository_count ? ` · ${node.repository_count} REPOS` : ""}`.toUpperCase()}
                </text>
              </g>
            );
          })}

          {allNodes.length === 0 && (
            <text x={WIDTH / 2} y={HEIGHT / 2} textAnchor="middle" fill="#849188" fontSize="16">
              No dependency data yet
            </text>
          )}
        </svg>
      </div>

      <div className="divide-y divide-white/[0.07] md:hidden">
        {edges.length > 0 ? edges.map((edge) => (
          <button
            key={edge.id}
            type="button"
            onClick={() => setSelectedId(edge.source)}
            className="block w-full px-4 py-3 text-left transition-colors hover:bg-white/[0.025]"
          >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
              <span className="max-w-[40%] truncate font-medium text-text-bright">{edge.source_name}</span>
              <span className="text-accent">→</span>
              <span className="max-w-[40%] truncate font-medium text-text-bright">{edge.target_name}</span>
            </div>
            <div className="mt-1.5 flex items-center justify-between gap-3 text-[10px] text-text-muted">
              <span className="uppercase tracking-wide">{edge.promotion_level ?? "structural"} · {edge.dependency_type}</span>
              <span className="font-mono">{edge.call_count} call sites</span>
            </div>
          </button>
        )) : allNodes.map((node) => (
          <button
            key={node.id}
            type="button"
            onClick={() => setSelectedId(node.id)}
            className="block w-full px-4 py-3 text-left text-[12px] font-medium text-text-bright hover:bg-white/[0.025]"
          >
            {node.name}
            {node.unresolved && <span className="ml-2 text-[10px] font-normal text-text-muted">Not indexed</span>}
          </button>
        ))}
      </div>

      {selected && (
        <div className="border-t border-white/[0.08] px-4 py-4 sm:px-5">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <div className="text-sm font-semibold text-text-bright">{selected.name}</div>
            <div className="text-[11px] text-text-muted">
              {selected.unresolved
                ? "Not present in the entity registry"
                : `${selected.entity_type ?? "Entity"}${selected.repository_count ? ` · observed in ${selected.repository_count} repositories` : ""}`}
            </div>
          </div>
          {selectedEdges.length > 0 ? (
            <div className="mt-3 grid gap-x-8 gap-y-2 sm:grid-cols-2">
              {selectedEdges.map((edge) => {
                const outgoing = sourceId(edge) === selected.id;
                return (
                  <div key={edge.id} className="flex min-w-0 items-center gap-2 text-[11px]">
                    <span className="font-mono text-accent">{outgoing ? "→" : "←"}</span>
                    <span className="truncate text-text-muted">
                      {outgoing ? edge.target_name : edge.source_name}
                    </span>
                    <span className="ml-auto shrink-0 text-text-dim">{edge.dependency_type}</span>
                    <span className="shrink-0 font-mono text-text-dim">×{edge.call_count}</span>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="mt-2 text-[11px] text-text-muted">No dependencies found for this node.</p>
          )}
        </div>
      )}
    </div>
  );
}
