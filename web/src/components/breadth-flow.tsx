"use client";

import dagre from "@dagrejs/dagre";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  Boxes,
  ChevronRight,
  CircleDot,
  Code2,
  Database,
  FileCode2,
  FolderTree,
  Network,
  RotateCcw,
  Search,
  Server,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { BreadthEdge, BreadthFlowGraph, BreadthNode } from "@/lib/api";

const NODE_WIDTH = 236;
const NODE_HEIGHT = 104;
const MAX_VISIBLE_NODES = 180;

interface BreadthNodeData extends Record<string, unknown> {
  breadth: BreadthNode;
  childrenCount: number;
}

type BreadthFlowNode = Node<BreadthNodeData, "breadth">;
type BreadthFlowEdge = Edge<{ breadth: BreadthEdge }>;

const KIND_ICONS: Record<string, typeof Code2> = {
  system: Network,
  service: Server,
  datastore: Database,
  queue: Boxes,
  external_api: Network,
  endpoint: CircleDot,
  module: FolderTree,
  function: Code2,
  file: FileCode2,
};

function kindColor(kind: string, layer?: string) {
  if (kind === "datastore") return "#87BDF5";
  if (kind === "queue") return "#F1C27B";
  if (kind === "external_api") return "#E597B4";
  if (layer === "infra") return "#8ECFC0";
  return "#C8F169";
}

function BreadthNodeCard({ data, selected }: NodeProps<BreadthFlowNode>) {
  const { breadth, childrenCount } = data;
  const Icon = KIND_ICONS[breadth.kind] ?? CircleDot;
  const color = kindColor(breadth.kind, breadth.layer);
  return (
    <div className={`relative w-[236px] rounded-lg border bg-[#111813] px-3.5 py-3 shadow-[0_12px_30px_rgba(0,0,0,0.24)] transition-colors ${selected ? "border-accent shadow-[0_0_22px_rgba(200,241,105,0.18)]" : "border-white/[0.11] hover:border-white/25"}`}>
      <Handle type="target" position={Position.Left} className="!h-2 !w-2 !border-2 !border-[#0B100D]" style={{ background: color }} />
      <div className="mb-2 flex items-center gap-2">
        <Icon size={14} style={{ color }} />
        <span className="text-[9px] font-bold uppercase tracking-[0.12em] text-text-muted">{breadth.kind.replaceAll("_", " ")}</span>
        {breadth.layer && <span className="ml-auto text-[9px] uppercase tracking-wide text-text-dim">{breadth.layer}</span>}
      </div>
      <div className="truncate text-[13px] font-semibold text-text-bright" title={breadth.name}>{breadth.name}</div>
      {breadth.source?.file && <div className="mt-1 truncate font-mono text-[9px] text-text-muted" title={breadth.source.file}>{breadth.source.file}{breadth.source.line ? `:${breadth.source.line}` : ""}</div>}
      <div className="mt-2 flex items-center justify-between gap-2 text-[9px] text-text-dim">
        <span className="truncate">{breadth.description || (breadth.aliases?.[0] ?? "")}</span>
        {childrenCount > 0 && <span className="shrink-0">{childrenCount} items ↗</span>}
      </div>
      <Handle type="source" position={Position.Right} className="!h-2 !w-2 !border-2 !border-[#0B100D]" style={{ background: color }} />
    </div>
  );
}

const nodeTypes = { breadth: BreadthNodeCard };

function visibleAncestor(nodeId: string, scopeId: string | null, nodeById: Map<string, BreadthNode>) {
  let node = nodeById.get(nodeId);
  const seen = new Set<string>();
  while (node && !seen.has(node.id)) {
    if ((node.parent ?? null) === scopeId) return node;
    seen.add(node.id);
    if (!node.parent) return undefined;
    node = nodeById.get(node.parent);
  }
  return undefined;
}

function layoutBreadthGraph(graph: BreadthFlowGraph, scopeId: string | null, layer: string, kind: string, search: string) {
  const nodeById = new Map(graph.nodes.map((node) => [node.id, node]));
  const children = graph.nodes
    .filter((node) => (node.parent ?? null) === scopeId)
    .filter((node) => layer === "all" || node.layer === layer)
    .filter((node) => kind === "all" || node.kind === kind)
    .filter((node) => !search || `${node.name} ${node.source?.file ?? ""} ${node.description ?? ""}`.toLowerCase().includes(search.toLowerCase()))
    .slice(0, MAX_VISIBLE_NODES);
  const visibleIds = new Set(children.map((node) => node.id));
  const childCounts = new Map<string, number>();
  for (const node of graph.nodes) if (node.parent) childCounts.set(node.parent, (childCounts.get(node.parent) ?? 0) + 1);

  const grouped = new Map<string, { source: string; target: string; edge: BreadthEdge; count: number }>();
  for (const edge of graph.edges) {
    const source = visibleAncestor(edge.from.node, scopeId, nodeById);
    const target = visibleAncestor(edge.to.node, scopeId, nodeById);
    if (!source || !target || source.id === target.id || !visibleIds.has(source.id) || !visibleIds.has(target.id)) continue;
    const key = `${source.id}\u0000${target.id}\u0000${edge.kind}\u0000${edge.label ?? ""}`;
    const old = grouped.get(key);
    if (old) old.count += edge.count ?? 1;
    else grouped.set(key, { source: source.id, target: target.id, edge, count: edge.count ?? 1 });
  }

  const layout = new dagre.graphlib.Graph().setDefaultEdgeLabel(() => ({}));
  layout.setGraph({ rankdir: "LR", nodesep: 42, ranksep: 108, marginx: 48, marginy: 40 });
  for (const node of children) layout.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const edge of grouped.values()) layout.setEdge(edge.source, edge.target, { weight: edge.count });
  dagre.layout(layout);

  const nodes: BreadthFlowNode[] = children.map((breadth) => {
    const position = layout.node(breadth.id);
    return {
      id: breadth.id,
      type: "breadth",
      position: { x: position.x - NODE_WIDTH / 2, y: position.y - NODE_HEIGHT / 2 },
      data: { breadth, childrenCount: childCounts.get(breadth.id) ?? 0 },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    };
  });
  const edges: BreadthFlowEdge[] = Array.from(grouped.values(), ({ source, target, edge, count }, index) => ({
    id: `${source}:${target}:${edge.kind}:${edge.label ?? index}`,
    source,
    target,
    type: "smoothstep",
    label: `${edge.label || edge.kind}${count > 1 ? ` ×${count}` : ""}`,
    labelStyle: { fill: "#A6B2A8", fontSize: 10 },
    labelBgStyle: { fill: "#0B100D", fillOpacity: 0.92 },
    style: { stroke: edge.kind === "event" ? "#C8F169" : edge.kind === "read" ? "#87BDF5" : "#8ECFC0", strokeWidth: 1.5 },
    markerEnd: { type: MarkerType.ArrowClosed, color: edge.kind === "event" ? "#C8F169" : "#8ECFC0" },
    data: { breadth: edge },
    focusable: true,
  }));
  return { nodes, edges, childCounts };
}

function BreadthCanvas({ graph }: { graph: BreadthFlowGraph }) {
  const rootNode = graph.nodes.find((node) => node.kind === "system" && !node.parent) ?? graph.nodes.find((node) => !node.parent);
  const rootId = rootNode?.id ?? null;
  const [scopeId, setScopeId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [layerFilter, setLayerFilter] = useState("all");
  const [kindFilter, setKindFilter] = useState("all");
  const [search, setSearch] = useState("");
  const { fitView } = useReactFlow();

  useEffect(() => { setScopeId(rootId); setSelectedId(null); }, [graph, rootId]);
  const { nodes: layoutNodes, edges, childCounts } = useMemo(() => layoutBreadthGraph(graph, scopeId, layerFilter, kindFilter, search), [graph, scopeId, layerFilter, kindFilter, search]);
  const [nodes, setNodes, onNodesChange] = useNodesState<BreadthFlowNode>([]);
  useEffect(() => { setNodes(layoutNodes); }, [layoutNodes, setNodes]);
  useEffect(() => {
    const timer = setTimeout(() => void fitView({ padding: 0.18, duration: 180 }), 0);
    return () => clearTimeout(timer);
  }, [fitView, nodes.length, scopeId]);

  const nodeById = useMemo(() => new Map(graph.nodes.map((node) => [node.id, node])), [graph.nodes]);
  const selected = selectedId ? nodeById.get(selectedId) : undefined;
  const layers = useMemo(() => Array.from(new Set(graph.nodes.map((node) => node.layer).filter(Boolean))).sort() as string[], [graph.nodes]);
  const kinds = useMemo(() => Array.from(new Set(graph.nodes.map((node) => node.kind))).sort(), [graph.nodes]);
  const breadcrumbs: BreadthNode[] = [];
  let parent = scopeId ? nodeById.get(scopeId) : undefined;
  while (parent) { breadcrumbs.unshift(parent); parent = parent.parent ? nodeById.get(parent.parent) : undefined; }

  function openNode(nodeId: string) {
    if (childCounts.get(nodeId)) {
      setScopeId(nodeId);
      setSelectedId(null);
      setLayerFilter("all");
      setKindFilter("all");
      setSearch("");
    } else setSelectedId(nodeId);
  }

  return (
    <div className="overflow-hidden rounded-lg border border-white/[0.09] bg-[#0D140F]">
      <div className="flex flex-wrap items-center gap-2 border-b border-white/[0.08] px-3 py-3 sm:px-4">
        <div className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto text-[11px]">
          <button type="button" onClick={() => { setScopeId(rootId); setSelectedId(null); }} className="shrink-0 text-text-muted hover:text-accent">Root</button>
          {breadcrumbs.map((node) => <span key={node.id} className="inline-flex shrink-0 items-center gap-1"><ChevronRight size={12} className="text-text-dim" /><button type="button" onClick={() => { setScopeId(node.id); setSelectedId(null); }} className="max-w-40 truncate text-text-muted hover:text-accent">{node.name}</button></span>)}
        </div>
        <label className="relative min-w-[150px] flex-1 sm:flex-none">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-text-dim" />
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Find in this level" className="w-full rounded-md border border-white/[0.09] bg-[#0B100D] py-1.5 pl-8 pr-2 text-[11px] text-text outline-none focus:border-accent/50 sm:w-44" />
        </label>
        <select aria-label="Filter by layer" value={layerFilter} onChange={(event) => setLayerFilter(event.target.value)} className="rounded-md border border-white/[0.09] bg-[#0B100D] px-2 py-1.5 text-[11px] text-text-muted"><option value="all">All layers</option>{layers.map((layer) => <option key={layer} value={layer}>{layer}</option>)}</select>
        <select aria-label="Filter by node kind" value={kindFilter} onChange={(event) => setKindFilter(event.target.value)} className="rounded-md border border-white/[0.09] bg-[#0B100D] px-2 py-1.5 text-[11px] text-text-muted"><option value="all">All kinds</option>{kinds.map((kind) => <option key={kind} value={kind}>{kind.replaceAll("_", " ")}</option>)}</select>
        <button type="button" aria-label="Reset node layout" title="Reset node layout" onClick={() => { setNodes(layoutNodes); void fitView({ padding: 0.18, duration: 180 }); }} className="rounded-md border border-white/[0.09] p-2 text-text-muted hover:text-accent"><RotateCcw size={14} /></button>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.07] px-4 py-2 text-[10px] text-text-muted">
        <div className="flex items-center gap-4"><span>{nodes.length}{nodes.length === MAX_VISIBLE_NODES ? "+" : ""} nodes in this layer</span><span>{edges.length} connections</span></div>
        <span className="font-mono text-text-dim">BREADTH FLOWGRAPH · {graph.version}</span>
      </div>
      <div className="grid min-h-[680px] grid-cols-1 xl:grid-cols-[minmax(0,1fr)_290px]">
        <div className="hidden h-[min(76vh,900px)] min-h-[560px] bg-[#0B100D] md:block">
          <ReactFlow key={`${scopeId}:${layerFilter}:${kindFilter}:${search}`} nodes={nodes} edges={edges} onNodesChange={onNodesChange} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.18, maxZoom: 1.1 }} minZoom={0.08} maxZoom={1.8} nodesDraggable nodesConnectable={false} edgesReconnectable={false} onNodeClick={(_, node) => setSelectedId(node.id)} onNodeDoubleClick={(_, node) => openNode(node.id)} onPaneClick={() => setSelectedId(null)} onlyRenderVisibleElements>
            <Background color="#506052" gap={24} size={1} />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable nodeColor={(node) => kindColor((node.data as BreadthNodeData).breadth.kind, (node.data as BreadthNodeData).breadth.layer)} maskColor="rgba(5, 9, 6, 0.72)" className="!border !border-white/[0.12] !bg-[#111813]" />
          </ReactFlow>
        </div>
        <div className="divide-y divide-white/[0.07] md:hidden">
          {nodes.map((node) => {
            const breadth = node.data.breadth;
            const Icon = KIND_ICONS[breadth.kind] ?? CircleDot;
            return (
              <button key={node.id} type="button" onClick={() => setSelectedId(node.id)} className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-white/[0.025]">
                <Icon size={15} className="shrink-0" style={{ color: kindColor(breadth.kind, breadth.layer) }} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12px] font-semibold text-text-bright">{breadth.name}</span>
                  <span className="mt-0.5 block truncate font-mono text-[9px] text-text-muted">{breadth.source?.file ?? breadth.kind}</span>
                </span>
                <span className="shrink-0 text-[9px] text-text-dim">{node.data.childrenCount ? `${node.data.childrenCount} children` : breadth.kind}</span>
              </button>
            );
          })}
          {nodes.length === 0 && <p className="px-4 py-8 text-center text-[11px] text-text-muted">No nodes match these filters.</p>}
        </div>
        <aside className="border-t border-white/[0.08] bg-[#101711] p-4 xl:border-l xl:border-t-0">
          {selected ? <NodeDetails node={selected} childCount={childCounts.get(selected.id) ?? 0} onOpen={() => openNode(selected.id)} /> : <div className="flex h-full min-h-36 flex-col justify-center text-center xl:text-left"><div className="mb-2 inline-flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.13em] text-text-muted"><CircleDot size={13} className="text-accent" /> Selected node</div><p className="text-[11px] leading-relaxed text-text-dim">Select a node to inspect its description, source, aliases, and ports. Open nodes with children to drill down.</p></div>}
          {!!graph.insights?.length && <div className="mt-5 border-t border-white/[0.08] pt-4"><div className="mb-2 text-[10px] font-bold uppercase tracking-[0.12em] text-text-muted">Breadth findings</div><div className="space-y-2">{graph.insights.slice(0, 8).map((insight, index) => <div key={`${insight.title}:${index}`} className="border-l-2 border-accent/60 pl-2.5"><div className="text-[11px] font-semibold text-text">{insight.title}</div>{insight.detail && <p className="mt-1 text-[10px] leading-relaxed text-text-muted">{insight.detail}</p>}</div>)}</div></div>}
        </aside>
      </div>
    </div>
  );
}

function NodeDetails({ node, childCount, onOpen }: { node: BreadthNode; childCount: number; onOpen: () => void }) {
  const Icon = KIND_ICONS[node.kind] ?? CircleDot;
  return (
    <div>
      <div className="mb-3 flex items-center gap-2"><Icon size={15} style={{ color: kindColor(node.kind, node.layer) }} /><span className="text-[10px] font-bold uppercase tracking-[0.13em] text-text-muted">{node.kind.replaceAll("_", " ")}</span>{node.layer && <span className="ml-auto rounded-sm bg-white/[0.05] px-1.5 py-0.5 text-[9px] uppercase text-text-dim">{node.layer}</span>}</div>
      <h2 className="break-words text-[16px] font-semibold leading-snug text-text-bright">{node.name}</h2>
      {node.description && <p className="mt-2 text-[11px] leading-relaxed text-text-muted">{node.description}</p>}
      {node.source?.file && <div className="mt-4 rounded-md border border-white/[0.08] bg-[#0B100D] p-2.5"><div className="mb-1 text-[9px] font-bold uppercase tracking-wide text-text-dim">Source</div><div className="break-all font-mono text-[10px] text-text-muted">{node.source.file}{node.source.line ? `:${node.source.line}` : ""}{node.source.endLine ? `–${node.source.endLine}` : ""}</div></div>}
      {node.aliases?.length ? <div className="mt-4"><div className="mb-1 text-[9px] font-bold uppercase tracking-wide text-text-dim">Aliases</div><ul className="space-y-1">{node.aliases.slice(0, 8).map((alias) => <li key={alias} className="break-all font-mono text-[9px] text-text-muted">{alias}</li>)}</ul></div> : null}
      {!!node.inputs?.length && <Ports title="Inputs" ports={node.inputs} />}
      {!!node.outputs?.length && <Ports title="Outputs" ports={node.outputs} />}
      {childCount > 0 && <button type="button" onClick={onOpen} className="mt-5 inline-flex w-full items-center justify-center gap-2 rounded-md bg-accent px-3 py-2 text-[11px] font-bold text-[#192110] hover:brightness-105"><FolderTree size={13} /> Open {childCount} children</button>}
    </div>
  );
}

function Ports({ title, ports }: { title: string; ports: { name: string; type?: string }[] }) {
  return <div className="mt-4"><div className="mb-1 text-[9px] font-bold uppercase tracking-wide text-text-dim">{title}</div><ul className="space-y-1">{ports.slice(0, 12).map((port, index) => <li key={`${port.name}:${index}`} className="flex min-w-0 items-center justify-between gap-2 text-[10px]"><span className="truncate text-text-muted">{port.name}</span>{port.type && <span className="shrink-0 font-mono text-text-dim">{port.type}</span>}</li>)}</ul></div>;
}

export function BreadthGraphCanvas({ graph }: { graph: BreadthFlowGraph }) {
  return <ReactFlowProvider><BreadthCanvas graph={graph} /></ReactFlowProvider>;
}
