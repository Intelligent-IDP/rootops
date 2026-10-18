"""
RootOps — Repositories API Router

CRUD endpoints for registered repositories plus the dependency graph
endpoint that powers the dashboard topology view.

Routes:
  GET  /api/repos                — list all repositories
  GET  /api/repos/graph          — dependency graph (nodes + edges)
  GET  /api/repos/{repo_id}      — single repository detail
  DELETE /api/repos/{repo_id}    — delete repo and all its data (CASCADE)
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.repository import Repository
from app.models.service_dependency import ServiceDependency

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/repos", tags=["repositories"])


# ── Response models ───────────────────────────────────────────────

class RepoSummary(BaseModel):
    id: str
    name: str
    url: str | None
    local_path: str | None
    team: str | None
    tags: list
    description: str | None
    last_ingested_at: str | None
    chunk_count: int
    commit_count: int


class GraphNode(BaseModel):
    id: str
    name: str
    team: str | None
    chunk_count: int
    commit_count: int
    last_ingested_at: str | None


class GraphEdge(BaseModel):
    id: str
    source: str          # source repo id
    target: str          # target repo id (may be None for unresolved)
    source_name: str
    target_name: str
    dependency_type: str
    call_count: int
    confidence: float


class DependencyGraph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("", response_model=list[RepoSummary])
async def list_repositories(session: AsyncSession = Depends(get_db)):
    """List all registered repositories, newest first."""
    rows = (
        await session.execute(
            select(Repository).order_by(Repository.created_at.desc())
        )
    ).scalars().all()
    return [_repo_to_summary(r) for r in rows]


@router.get("/graph", response_model=DependencyGraph)
async def get_dependency_graph(session: AsyncSession = Depends(get_db)):
    """Return the service dependency graph for the dashboard.

    Nodes are repositories. Edges are detected cross-service dependencies.
    Unresolved targets (services not yet ingested) appear as orphan targets
    in edge data but are NOT included as nodes to keep the graph clean.
    """
    repos = (await session.execute(select(Repository))).scalars().all()
    deps  = (await session.execute(select(ServiceDependency))).scalars().all()

    # Build a set of known repo IDs to filter edges
    repo_ids = {r.id for r in repos}

    nodes = [_repo_to_node(r) for r in repos]

    edges: list[GraphEdge] = []
    for dep in deps:
        # Only emit edges where we know both endpoints
        if dep.target_repo_id and dep.target_repo_id in repo_ids:
            edges.append(GraphEdge(
                id=str(dep.id),
                source=str(dep.source_repo_id),
                target=str(dep.target_repo_id),
                source_name=dep.source_repo_name,
                target_name=dep.target_repo_name,
                dependency_type=dep.dependency_type,
                call_count=dep.call_count,
                confidence=dep.confidence,
            ))
        elif not dep.target_repo_id:
            # Include unresolved edges with a sentinel target id = ""
            edges.append(GraphEdge(
                id=str(dep.id),
                source=str(dep.source_repo_id),
                target="",
                source_name=dep.source_repo_name,
                target_name=dep.target_repo_name,
                dependency_type=dep.dependency_type,
                call_count=dep.call_count,
                confidence=dep.confidence,
            ))

    return DependencyGraph(nodes=nodes, edges=edges)


@router.get("/{repo_id}", response_model=RepoSummary)
async def get_repository(
    repo_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
):
    """Get a single repository by ID."""
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, f"Repository {repo_id} not found")
    return _repo_to_summary(repo)


@router.delete("/{repo_id}", status_code=204)
async def delete_repository(
    repo_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
):
    """Delete a repository and all its associated data (CASCADE).

    This removes all code chunks, commits, log entries, ingestion state,
    codebase summary, and dependency edges for this repository.
    """
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, f"Repository {repo_id} not found")
    await session.delete(repo)
    await session.commit()


# ── Helpers ───────────────────────────────────────────────────────

def _repo_to_summary(r: Repository) -> RepoSummary:
    return RepoSummary(
        id=str(r.id),
        name=r.name,
        url=r.url,
        local_path=r.local_path,
        team=r.team,
        tags=r.tags or [],
        description=r.description,
        last_ingested_at=r.last_ingested_at.isoformat() if r.last_ingested_at else None,
        chunk_count=r.chunk_count,
        commit_count=r.commit_count,
    )


def _repo_to_node(r: Repository) -> GraphNode:
    return GraphNode(
        id=str(r.id),
        name=r.name,
        team=r.team,
        chunk_count=r.chunk_count,
        commit_count=r.commit_count,
        last_ingested_at=r.last_ingested_at.isoformat() if r.last_ingested_at else None,
    )
