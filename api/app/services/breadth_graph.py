"""
RootOps — Breadth dependency graph

Replaces the regex dependency extractor with Breadth
(https://github.com/theorjiugovictor/breadth), which parses code with
tree-sitter and also reads Terraform, Kubernetes and OpenTelemetry traces.

Breadth writes a flowgraph.json down to function level. RootOps does not
need that much detail for causation, so this module rolls the graph up to
the entities in the RootOps ontology (Service, Database, Queue, Topic, API)
and writes three things:

  1. EntityRegistry rows, with Breadth's ids and aliases as aliases, so log
     service names, Kubernetes names and code names resolve to one entity.
  2. GraphEdge rows with promotion_level = "structural". These are facts
     about how the system is wired (calls, reads_from, writes_to,
     publishes_to, subscribes_to, connects_to). They are never promoted and
     never appear in causal chains, but they give the causation service a
     real topology to check "is there a path from A to B?" against.
  3. ServiceDependency rows, so the dashboard graph and the auto-heal blast
     radius keep working unchanged.

Each import is a full snapshot for one repository: structural edges and
dependency rows from the previous scan of that repository are replaced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger(__name__)

STRUCTURAL = "structural"
EVIDENCE_SOURCE = "breadth"

# Breadth node kind -> RootOps entity type. Datastores that Breadth found
# as in-process state (layer "code", e.g. a module-level cache) are not
# system entities and are skipped.
_ENTITY_KINDS = {
    "service": "Service",
    "datastore": "Database",
    "queue": "Queue",
    "external_api": "API",
}


@dataclass(frozen=True)
class Entity:
    breadth_id: str
    name: str
    entity_type: str
    aliases: tuple[str, ...] = ()


@dataclass
class StructuralEdge:
    source: Entity
    target: Entity
    edge_type: str
    protocol: str | None = None
    count: int = 0
    files: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)


@dataclass
class RolledUpGraph:
    entities: dict[str, Entity]
    edges: list[StructuralEdge]
    nodes_in: int
    edges_in: int


# ── Pure roll-up (no I/O, unit tested) ────────────────────────────

def _entity_type(node: dict, nodes: dict[str, dict]) -> str | None:
    kind = node.get("kind")
    if kind == "datastore" and node.get("layer") == "code":
        return None
    if kind == "queue" and nodes.get(node.get("parent") or "", {}).get("kind") == "queue":
        # queue:kafka:order-created sits inside queue:kafka -> a topic
        return "Topic"
    return _ENTITY_KINDS.get(kind)


def _orient(kind: str, a: Entity, b: Entity) -> tuple[Entity, Entity, str] | None:
    """Return (source, target, edge_type) so that source depends on target."""
    if kind == "call":
        return a, b, "calls"
    if kind == "write":
        return a, b, "writes_to"
    if kind == "read":
        # Breadth draws reads as data flowing store -> reader.
        if a.entity_type in ("Database", "Queue", "Topic", "API"):
            return b, a, "reads_from"
        return a, b, "reads_from"
    if kind == "event":
        if a.entity_type in ("Queue", "Topic"):
            return b, a, "subscribes_to"
        return a, b, "publishes_to"
    if kind in ("network", "data"):
        return a, b, "connects_to"
    if kind == "depends_on":
        return a, b, "depends_on"
    return None


def roll_up(flowgraph: dict) -> RolledUpGraph:
    """Collapse a Breadth flowgraph to entity-level structural edges."""
    nodes = {n["id"]: n for n in flowgraph.get("nodes", [])}
    alias_to_id: dict[str, str] = {}
    for n in nodes.values():
        for a in n.get("aliases") or []:
            alias_to_id.setdefault(a, n["id"])

    cache: dict[str, Entity | None] = {}

    def entity_for(node_id: str) -> Entity | None:
        node_id = alias_to_id.get(node_id, node_id)
        if node_id in cache:
            return cache[node_id]
        seen: set[str] = set()
        cur = node_id
        found: Entity | None = None
        while cur in nodes and cur not in seen:
            seen.add(cur)
            n = nodes[cur]
            etype = _entity_type(n, nodes)
            if etype:
                found = Entity(
                    breadth_id=n["id"],
                    name=n["name"],
                    entity_type=etype,
                    aliases=tuple(sorted({n["id"], *(n.get("aliases") or [])})),
                )
                break
            cur = n.get("parent")
        cache[node_id] = found
        return found

    entities: dict[str, Entity] = {}
    merged: dict[tuple[str, str, str], StructuralEdge] = {}

    for e in flowgraph.get("edges", []):
        a = entity_for(e["from"]["node"])
        b = entity_for(e["to"]["node"])
        if not a or not b or a.name == b.name:
            continue
        oriented = _orient(e.get("kind", ""), a, b)
        if not oriented:
            continue
        src, tgt, etype = oriented
        key = (src.name, tgt.name, etype)
        edge = merged.get(key)
        if edge is None:
            edge = merged[key] = StructuralEdge(src, tgt, etype, protocol=e.get("protocol"))
        edge.count += int(e.get("count") or 1)
        meta = e.get("metadata") or {}
        if isinstance(meta.get("file"), str):
            edge.files.add(meta["file"])
        if e.get("label"):
            edge.labels.add(e["label"])
        entities[src.name] = src
        entities[tgt.name] = tgt

    # Keep every system-level entity, even ones with no edges yet.
    for nid in nodes:
        ent = entity_for(nid)
        if ent and ent.breadth_id == nid:
            entities.setdefault(ent.name, ent)

    return RolledUpGraph(
        entities=entities,
        edges=sorted(merged.values(), key=lambda x: (x.source.name, x.target.name, x.edge_type)),
        nodes_in=len(nodes),
        edges_in=len(flowgraph.get("edges", [])),
    )


def _dependency_type(edge: StructuralEdge) -> str | None:
    """Map a structural edge to the dashboard's ServiceDependency types."""
    if edge.source.entity_type != "Service":
        return None
    if edge.edge_type in ("publishes_to", "subscribes_to"):
        return "event"
    if edge.edge_type in ("calls", "connects_to") and edge.target.entity_type in ("Service", "API"):
        return "grpc" if (edge.protocol or "").lower() == "grpc" else "http"
    return None


async def _persist_flowgraph_snapshot(
    session: AsyncSession,
    flowgraph: dict,
    repo_id: uuid.UUID | None,
) -> None:
    """Replace the repository's detailed Breadth graph snapshot."""
    if repo_id is None:
        return

    from app.models.breadth_graph_snapshot import BreadthGraphSnapshot

    snapshot = await session.get(BreadthGraphSnapshot, repo_id)
    if snapshot is None:
        session.add(BreadthGraphSnapshot(repo_id=repo_id, flowgraph=flowgraph))
    else:
        snapshot.flowgraph = flowgraph
        snapshot.scanned_at = datetime.now(UTC)


# ── Running the Breadth CLI ───────────────────────────────────────

def breadth_command() -> list[str] | None:
    """Return the command that runs Breadth, or None if it is not installed."""
    configured = get_settings().BREADTH_BIN.strip()
    if configured.endswith((".mjs", ".js")):
        node = shutil.which("node")
        return [node, configured] if node and os.path.exists(configured) else None
    found = shutil.which(configured or "breadth")
    return [found] if found else None


def _breadth_llm_options(settings) -> tuple[list[str], dict[str, str]]:
    """Configure Breadth's optional summaries from the selected RootOps LLM."""
    if not settings.BREADTH_LLM_ENABLED or not settings.LLM_AVAILABLE:
        return [], {}

    backend = settings.LLM_BACKEND.lower()
    providers = {
        "ollama": ("ollama", settings.OLLAMA_MODEL, settings.OLLAMA_BASE_URL),
        "openai": ("openai", settings.OPENAI_MODEL, None),
        "anthropic": ("anthropic", settings.ANTHROPIC_MODEL, None),
        "gemini": ("gemini", settings.GEMINI_MODEL, None),
    }
    configured = providers.get(backend)
    if configured is None:
        logger.warning(
            "Breadth LLM summaries are unavailable for LLM_BACKEND=%s",
            backend,
        )
        return [], {}

    provider, model, base_url = configured
    env = {
        "BREADTH_LLM_PROVIDER": provider,
        "BREADTH_LLM_MODEL": model,
    }
    if base_url:
        env["BREADTH_LLM_BASE_URL"] = base_url
    key_names = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    key_name = key_names.get(backend)
    if key_name:
        key = getattr(settings, key_name)
        if key:
            env[key_name] = key
    return ["--llm"], env


async def scan_repository(repo_path: str) -> dict | None:
    """Run `breadth scan` on a checked-out repository and return the flowgraph."""
    cmd = breadth_command()
    if not cmd:
        return None
    settings = get_settings()
    timeout = settings.BREADTH_SCAN_TIMEOUT_SECONDS
    llm_args, llm_env = _breadth_llm_options(settings)
    with tempfile.TemporaryDirectory(prefix="breadth-") as tmp:
        out = os.path.join(tmp, "flowgraph.json")
        async def run_scan(args: list[str], env: dict[str, str]) -> tuple[int, bytes]:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return -1, f"timed out after {timeout}s".encode()
            return proc.returncode or 0, stderr

        scan_args = [*cmd, "scan", repo_path, "-o", out]
        return_code, stderr = await run_scan(
            [*scan_args, *llm_args],
            {**os.environ, **llm_env},
        )
        if return_code != 0 and llm_args:
            logger.warning(
                "Breadth scan with LLM summaries failed (%s); retrying without LLM: %s",
                return_code,
                stderr.decode(errors="replace")[-500:],
            )
            return_code, stderr = await run_scan(scan_args, os.environ.copy())

        if return_code != 0 or not os.path.exists(out):
            logger.warning(
                "breadth scan failed (%s): %s",
                return_code,
                stderr.decode(errors="replace")[-500:],
            )
            return None
        return await asyncio.to_thread(_read_json, out)


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Persisting ────────────────────────────────────────────────────

async def import_flowgraph(
    session: AsyncSession,
    flowgraph: dict,
    *,
    repo_id: uuid.UUID | None,
    repo_name: str,
) -> dict:
    """Write a Breadth flowgraph into the knowledge graph for one repository.

    Caller commits.
    """
    from app.models.entity_registry import EntityRegistry
    from app.models.graph_edge import GraphEdge
    from app.models.repository import Repository
    from app.models.service_dependency import ServiceDependency

    if flowgraph.get("version") != "1.0" or not isinstance(flowgraph.get("nodes"), list):
        raise ValueError("not a Breadth flowgraph (expected version 1.0 with nodes and edges)")

    await _persist_flowgraph_snapshot(session, flowgraph, repo_id)
    graph = roll_up(flowgraph)
    now = datetime.now(UTC)
    repo_key = str(repo_id) if repo_id else repo_name

    # 1. Entities: merge aliases, never drop existing ones.
    existing = {
        e.canonical_name: e
        for e in (await session.execute(
            select(EntityRegistry).where(EntityRegistry.canonical_name.in_(list(graph.entities)))
        )).scalars().all()
    } if graph.entities else {}
    for name, ent in graph.entities.items():
        row = existing.get(name)
        if row is None:
            session.add(EntityRegistry(
                canonical_name=name,
                entity_type=ent.entity_type,
                aliases=list(ent.aliases),
                repos=[repo_key],
                metadata_json={"breadth_id": ent.breadth_id},
            ))
        else:
            row.aliases = sorted(set(row.aliases or []) | set(ent.aliases))
            row.repos = sorted(set(row.repos or []) | {repo_key})
            row.last_seen_at = now
            row.metadata_json = {**(row.metadata_json or {}), "breadth_id": ent.breadth_id}

    # 2. Structural edges. The same wire (say orders calls payments) can be
    #    seen from several repositories, and (source, target, edge_type) is
    #    unique, so each row keeps the list of repositories that reported it.
    #    This repository's previous snapshot is withdrawn first.
    rows = (await session.execute(select(GraphEdge))).scalars().all()
    by_key = {(r.source_entity, r.target_entity, r.edge_type): r for r in rows}
    for row in rows:
        ev = row.evidence or {}
        if row.promotion_level != STRUCTURAL or ev.get("source") != EVIDENCE_SOURCE:
            continue
        repos = [r for r in ev.get("repos", []) if r != repo_key]
        if repos:
            row.evidence = {**ev, "repos": repos}
        else:
            await session.delete(row)
            del by_key[(row.source_entity, row.target_entity, row.edge_type)]
    await session.flush()

    for edge in graph.edges:
        evidence = {
            "source": EVIDENCE_SOURCE,
            "protocol": edge.protocol,
            "call_sites": edge.count,
            "files": sorted(edge.files)[:20],
            "labels": sorted(edge.labels)[:20],
            "breadth_from": edge.source.breadth_id,
            "breadth_to": edge.target.breadth_id,
        }
        key = (edge.source.name, edge.target.name, edge.edge_type)
        row = by_key.get(key)
        if row is None:
            session.add(GraphEdge(
                source_entity=edge.source.name,
                source_type=edge.source.entity_type,
                target_entity=edge.target.name,
                target_type=edge.target.entity_type,
                edge_type=edge.edge_type,
                promotion_level=STRUCTURAL,
                confidence=1.0,
                incident_count=0,
                evidence={**evidence, "repos": [repo_key]},
            ))
        elif row.promotion_level == STRUCTURAL:
            repos = sorted(set((row.evidence or {}).get("repos", [])) | {repo_key})
            row.evidence = {**evidence, "repos": repos}
            row.last_seen_at = now
        # Any other row with this key is causal evidence: leave it alone.

    # 3. Dashboard dependency rows for this repository (full replace).
    deps_written = 0
    if repo_id is not None:
        await session.execute(
            delete(ServiceDependency).where(ServiceDependency.source_repo_id == repo_id)
        )
        repo_rows = (await session.execute(select(Repository.name, Repository.id))).fetchall()
        name_to_id = {r.name.lower(): r.id for r in repo_rows}
        seen: set[tuple[str, str]] = set()
        for edge in graph.edges:
            dep_type = _dependency_type(edge)
            if not dep_type:
                continue
            target = edge.target.name.lower()
            source_file = min(edge.files) if edge.files else f"breadth:{edge.source.name}"
            key = (target, dep_type)
            if target == repo_name.lower() or key in seen:
                continue
            seen.add(key)
            session.add(ServiceDependency(
                id=uuid.uuid4(),
                source_repo_id=repo_id,
                source_repo_name=repo_name,
                target_repo_name=target,
                target_repo_id=name_to_id.get(target),
                dependency_type=dep_type,
                source_file=source_file,
                source_pattern=(
                    f"breadth: {edge.source.name} {edge.edge_type} {edge.target.name}"
                    + (f" ({edge.protocol})" if edge.protocol else "")
                )[:200],
                call_count=max(edge.count, 1),
                confidence=1.0,
            ))
            deps_written += 1

    await session.flush()
    stats = {
        "nodes_in": graph.nodes_in,
        "edges_in": graph.edges_in,
        "entities": len(graph.entities),
        "structural_edges": len(graph.edges),
        "dependencies": deps_written,
    }
    logger.info("Breadth graph imported for %s: %s", repo_name, stats)
    return stats
