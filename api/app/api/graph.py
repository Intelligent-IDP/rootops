"""
RootOps — Knowledge Graph API Routes

Exposes GraphEdge and EntityRegistry data via REST.
Includes causation confirmation and entity alias management.
"""

from __future__ import annotations

from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from app.db import get_db
from app.services.causation_service import (
    confirm_causation,
    get_all_edges,
    get_causal_chain,
    record_co_occurrence,
)

router = APIRouter(prefix="/api/graph", tags=["graph"])


# ── Edges ─────────────────────────────────────────────────────────

@router.get("/edges")
async def list_edges(
    min_level: str = Query(
        "observed",
        description="Minimum promotion level: observed | correlates_with | probable_cause | confirmed_cause",
    ),
    session=Depends(get_db),
):
    """Return all knowledge graph edges at or above the specified promotion level."""
    return await get_all_edges(session, min_promotion_level=min_level)


@router.get("/edges/causal-chain")
async def causal_chain(
    entity: str = Query(..., description="Entity name to start traversal from"),
    direction: str = Query("upstream", description="upstream | downstream"),
    depth: int = Query(5, ge=1, le=10),
    min_level: str = Query("correlates_with"),
    session=Depends(get_db),
):
    """Return the causal chain for an entity (upstream causes or downstream effects)."""
    return await get_causal_chain(
        session,
        entity=entity,
        direction=direction,
        min_promotion_level=min_level,
        max_depth=depth,
    )


class CoOccurrenceRequest(BaseModel):
    source_entity: str
    source_type: str = "Service"
    target_entity: str
    target_type: str = "Service"
    temporal_lag_ms: float | None = None
    correlation_window_seconds: int = 300


@router.post("/edges/co-occurrence")
async def record_co_occurrence_endpoint(
    body: CoOccurrenceRequest,
    session=Depends(get_db),
):
    """
    Record a co-occurrence between two entities and evaluate edge promotion.
    Called by monitoring systems when two events are observed close in time.
    """
    edge = await record_co_occurrence(
        session,
        source_entity=body.source_entity,
        source_type=body.source_type,
        target_entity=body.target_entity,
        target_type=body.target_type,
        temporal_lag_ms=body.temporal_lag_ms,
        correlation_window_seconds=body.correlation_window_seconds,
    )
    await session.commit()
    return edge.to_dict()


class ConfirmCausationRequest(BaseModel):
    source_entity: str
    target_entity: str
    confirmed_by: str  # email or username of the engineer confirming


@router.post("/edges/confirm")
async def confirm_causation_endpoint(
    body: ConfirmCausationRequest,
    session=Depends(get_db),
):
    """
    Promote an edge to confirmed_cause. Requires a human confirmer.
    This is the only path to confirmed_cause — the system cannot self-promote.
    """
    edge = await confirm_causation(
        session,
        source_entity=body.source_entity,
        target_entity=body.target_entity,
        confirmed_by=body.confirmed_by,
    )
    if edge is None:
        raise HTTPException(
            status_code=404,
            detail=f"No correlates_with or probable_cause edge found from "
                   f"'{body.source_entity}' to '{body.target_entity}'",
        )
    await session.commit()
    return edge.to_dict()


# ── Entity Registry ───────────────────────────────────────────────

@router.get("/entities")
async def list_entities(
    entity_type: str | None = Query(None),
    deprecated: bool = Query(False),
    session=Depends(get_db),
):
    """List entities from the canonical registry."""
    from sqlalchemy import select

    from app.models.entity_registry import EntityRegistry

    query = select(EntityRegistry).where(EntityRegistry.deprecated == deprecated)
    if entity_type:
        query = query.where(EntityRegistry.entity_type == entity_type)
    result = await session.execute(query.order_by(EntityRegistry.canonical_name))
    return [e.to_dict() for e in result.scalars().all()]


class EntityUpsertRequest(BaseModel):
    canonical_name: str
    entity_type: str
    aliases: list[str] = []
    repos: list[str] = []
    description: str | None = None
    metadata_json: dict | None = None


@router.post("/entities")
async def upsert_entity(body: EntityUpsertRequest, session=Depends(get_db)):
    """Create or update an entity in the canonical registry."""
    from datetime import datetime

    from sqlalchemy import select

    from app.models.entity_registry import EntityRegistry

    result = await session.execute(
        select(EntityRegistry).where(
            EntityRegistry.canonical_name == body.canonical_name
        )
    )
    entity = result.scalar_one_or_none()

    if entity is None:
        entity = EntityRegistry(
            canonical_name=body.canonical_name,
            entity_type=body.entity_type,
            aliases=body.aliases or [],
            repos=body.repos or [],
            description=body.description,
            metadata_json=body.metadata_json,
        )
        session.add(entity)
    else:
        # Merge aliases without duplicates
        existing = set(entity.aliases or [])
        entity.aliases = list(existing | set(body.aliases))
        existing_repos = set(entity.repos or [])
        entity.repos = list(existing_repos | set(body.repos))
        entity.last_seen_at = datetime.now(tz=UTC)
        if body.description:
            entity.description = body.description
        if body.metadata_json:
            entity.metadata_json = {**(entity.metadata_json or {}), **body.metadata_json}

    await session.commit()
    return entity.to_dict()


@router.post("/entities/{canonical_name}/deprecate")
async def deprecate_entity(canonical_name: str, session=Depends(get_db)):
    """Mark an entity as deprecated. History and edges are preserved."""
    from datetime import datetime

    from sqlalchemy import select

    from app.models.entity_registry import EntityRegistry

    result = await session.execute(
        select(EntityRegistry).where(EntityRegistry.canonical_name == canonical_name)
    )
    entity = result.scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail=f"Entity '{canonical_name}' not found")

    entity.deprecated = True
    entity.deprecated_at = datetime.now(tz=UTC)
    await session.commit()
    return entity.to_dict()


# ── Deployment event webhook ──────────────────────────────────────

class DeploymentEvent(BaseModel):
    service_name: str
    version: str | None = None
    environment: str = "production"
    repo_id: str | None = None
    deployed_by: str | None = None
    timestamp: str | None = None


@router.post("/events/deployment")
async def ingest_deployment_event(body: DeploymentEvent, session=Depends(get_db)):
    """
    Ingest a deployment event from CI/CD.
    Updates the EntityRegistry and seeds a Deployment node in the graph.
    Wire this endpoint into your CI/CD pipeline or Argo CD webhooks.
    """
    from datetime import datetime

    from sqlalchemy import select

    from app.models.entity_registry import EntityRegistry

    now = datetime.now(tz=UTC)

    # Upsert the service entity
    result = await session.execute(
        select(EntityRegistry).where(
            EntityRegistry.canonical_name == body.service_name
        )
    )
    entity = result.scalar_one_or_none()
    if entity is None:
        entity = EntityRegistry(
            canonical_name=body.service_name,
            entity_type="Service",
            aliases=[],
            repos=[body.repo_id] if body.repo_id else [],
            metadata_json={"last_version": body.version, "environment": body.environment},
        )
        session.add(entity)
    else:
        entity.last_seen_at = now
        meta = dict(entity.metadata_json or {})
        meta["last_version"] = body.version
        meta["environment"] = body.environment
        entity.metadata_json = meta
        if body.repo_id and body.repo_id not in (entity.repos or []):
            entity.repos = list(entity.repos or []) + [body.repo_id]

    # Record a Deployment → Service edge for the graph
    deployment_node = f"deployment:{body.service_name}:{body.version or 'unknown'}"
    await record_co_occurrence(
        session,
        source_entity=deployment_node,
        source_type="Deployment",
        target_entity=body.service_name,
        target_type="Service",
        temporal_lag_ms=0,
    )

    await session.commit()
    return {
        "status": "recorded",
        "service": body.service_name,
        "version": body.version,
        "environment": body.environment,
        "entity": entity.to_dict(),
    }


# ── Breadth flowgraph import ──────────────────────────────────────

@router.post("/breadth")
async def import_breadth_flowgraph(
    repo: str = Query(..., description="Repository name the graph was scanned from"),
    flowgraph: dict = Body(..., description="A Breadth flowgraph.json (version 1.0)"),
    session=Depends(get_db),
):
    """
    Import a Breadth flowgraph produced outside RootOps, for example by
    `breadth scan .` on a laptop or by the Breadth GitHub Action.

    Replaces this repository's previous structural edges and dependency rows.
    """
    from sqlalchemy import func, select

    from app.models.repository import Repository
    from app.services.breadth_graph import import_flowgraph

    row = (await session.execute(
        select(Repository).where(func.lower(Repository.name) == repo.lower())
    )).scalar_one_or_none()
    try:
        stats = await import_flowgraph(
            session, flowgraph,
            repo_id=row.id if row else None,
            repo_name=row.name if row else repo,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await session.commit()
    return {"status": "imported", "repo": repo, "repo_registered": row is not None, **stats}
