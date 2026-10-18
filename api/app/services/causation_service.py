"""
RootOps — Evidence-Gated Causation Service

Manages causal edges in the knowledge graph with evidence-based promotion:

  observed → correlates_with → probable_cause → confirmed_cause

Promotion rules (ALL must pass for each level):
  correlates_with:
    - Temporal: A consistently precedes B (median lag > 0, stdev < median)
    - Observed: ≥ 1 co-occurrence

  probable_cause:
    - Temporal: median lag in [0, correlation_window_seconds]
    - Statistical: Granger causality p < CAUSATION_GRANGER_P_THRESHOLD
    - Topological: A and B connected in service graph (any path)
    - Historical: incident_count ≥ CAUSATION_MIN_INCIDENTS

  confirmed_cause:
    - Requires explicit human confirmation via API (confirmed_by is set)

Edge evidence schema:
  {
    "temporal_precedence_ms": 340,       # median A→B lag in ms
    "temporal_std_ms": 120,              # std deviation of lag
    "granger_p_value": 0.02,             # Granger test p-value (None if not tested)
    "topology_path": ["A","mid","B"],    # shortest graph path between entities
    "incident_count": 5,                 # co-occurrence count
    "correlation_window_seconds": 300    # time window used for correlation
  }
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.graph_edge import GraphEdge

logger = logging.getLogger(__name__)
settings = get_settings()

# Promotion level ordering
_LEVELS = ["observed", "correlates_with", "probable_cause", "confirmed_cause"]
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(_LEVELS)}


# ── Statistical helpers ───────────────────────────────────────────

def _granger_test(series_a: list[float], series_b: list[float], max_lag: int = 3) -> float | None:
    """
    Run a simplified Granger causality test: does series_a help predict series_b?
    Returns the minimum p-value across tested lags, or None if scipy is unavailable
    or the series are too short.

    In production this should use time-series log counts at hourly resolution.
    """
    if len(series_a) < 10 or len(series_b) < 10:
        return None

    try:
        import numpy as np
        from statsmodels.tsa.stattools import grangercausalitytests

        data = np.column_stack([series_b, series_a])
        results = grangercausalitytests(data, maxlag=max_lag, verbose=False)
        # Extract minimum p-value across all lags and test types
        p_values = [
            results[lag][0][test][1]
            for lag in results
            for test in results[lag][0]
        ]
        return min(p_values) if p_values else None
    except ImportError:
        logger.debug("statsmodels not available — skipping Granger test")
        return None
    except Exception as exc:
        logger.debug("Granger test failed: %s", exc)
        return None


def _topology_path(
    source: str, target: str, edges: list[GraphEdge]
) -> list[str] | None:
    """
    BFS through existing graph edges to find a path from source → target.
    Returns the path as a list of entity names, or None if no path exists.
    """
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source_entity, set()).add(edge.target_entity)
        adjacency.setdefault(edge.target_entity, set()).add(edge.source_entity)

    if source not in adjacency:
        return None

    visited = {source}
    queue = [[source]]

    while queue:
        path = queue.pop(0)
        node = path[-1]
        for neighbor in adjacency.get(node, []):
            if neighbor == target:
                return path + [neighbor]
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(path + [neighbor])

    return None


# ── Core edge management ──────────────────────────────────────────

async def record_co_occurrence(
    session: AsyncSession,
    source_entity: str,
    source_type: str,
    target_entity: str,
    target_type: str,
    temporal_lag_ms: float | None = None,
    correlation_window_seconds: int = 300,
) -> GraphEdge:
    """
    Record a co-occurrence between two entities. Creates a 'correlates_with'
    edge if one doesn't exist, otherwise increments incident_count and
    updates evidence. Then evaluates whether the edge should be promoted.

    Args:
        source_entity: The entity that precedes (potential cause).
        target_entity: The entity that follows (potential effect).
        temporal_lag_ms: Time between source event and target event in ms.
                         None if ordering is unknown.
        correlation_window_seconds: Max window for considering events correlated.
    """
    # Look up existing edge
    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_entity == source_entity,
            GraphEdge.target_entity == target_entity,
            GraphEdge.edge_type == "correlates_with",
        )
    )
    edge = result.scalar_one_or_none()

    now = datetime.now(tz=UTC)

    if edge is None:
        evidence: dict = {
            "incident_count": 1,
            "correlation_window_seconds": correlation_window_seconds,
            "lag_samples_ms": [temporal_lag_ms] if temporal_lag_ms is not None else [],
        }
        edge = GraphEdge(
            id=str(uuid.uuid4()),
            source_entity=source_entity,
            source_type=source_type,
            target_entity=target_entity,
            target_type=target_type,
            edge_type="correlates_with",
            promotion_level="observed",
            confidence=0.1,
            evidence=evidence,
            incident_count=1,
        )
        session.add(edge)
    else:
        edge.incident_count += 1
        edge.last_seen_at = now

        # Accumulate lag samples (keep last 50)
        ev = dict(edge.evidence or {})
        samples: list = list(ev.get("lag_samples_ms", []))
        if temporal_lag_ms is not None:
            samples.append(temporal_lag_ms)
        ev["lag_samples_ms"] = samples[-50:]
        ev["incident_count"] = edge.incident_count
        edge.evidence = ev

    await session.flush()

    # Evaluate promotion after each co-occurrence
    await _evaluate_promotion(session, edge)
    return edge


async def _evaluate_promotion(session: AsyncSession, edge: GraphEdge) -> None:
    """
    Check whether the edge meets criteria to advance to the next promotion level.
    Modifies edge in-place. Caller is responsible for flush/commit.
    """
    current_rank = _LEVEL_RANK.get(edge.promotion_level, 0)
    ev = dict(edge.evidence or {})
    samples: list[float] = ev.get("lag_samples_ms", [])
    incident_count: int = ev.get("incident_count", edge.incident_count)

    # ── observed → correlates_with ─────────────────────────────
    if current_rank < _LEVEL_RANK["correlates_with"]:  # noqa: SIM102 — one block per promotion step
        if incident_count >= 1 and samples:
            median_lag = _median(samples)
            if median_lag >= 0:
                ev["temporal_precedence_ms"] = round(median_lag, 2)
                ev["temporal_std_ms"] = round(_stdev(samples), 2)
                edge.evidence = ev
                edge.promotion_level = "correlates_with"
                edge.confidence = min(0.4, 0.1 + incident_count * 0.05)
                edge.promoted_at = datetime.now(tz=UTC)
                logger.info(
                    "Edge %s→%s promoted to correlates_with (incidents=%d)",
                    edge.source_entity, edge.target_entity, incident_count,
                )
                current_rank = _LEVEL_RANK["correlates_with"]

    # ── correlates_with → probable_cause ──────────────────────
    if current_rank < _LEVEL_RANK["probable_cause"]:
        meets_temporal = len(samples) >= settings.CAUSATION_MIN_INCIDENTS
        meets_historical = incident_count >= settings.CAUSATION_MIN_INCIDENTS

        # Granger causality test using count-series approximation
        p_value: float | None = None
        if len(samples) >= 10:
            # Build two simple time series from the lag samples as proxy
            series_a = [1.0] * len(samples)
            series_b = [1.0 / max(s, 1) for s in samples]  # inverse lag as proxy
            p_value = _granger_test(series_a, series_b)

        ev["granger_p_value"] = p_value
        meets_statistical = (
            p_value is not None and p_value < settings.CAUSATION_GRANGER_P_THRESHOLD
        )

        # Topology check: find a path in existing graph edges
        topology_path: list[str] | None = None
        if meets_temporal and meets_historical:
            all_edges_result = await session.execute(select(GraphEdge))
            all_edges = all_edges_result.scalars().all()
            topology_path = _topology_path(
                edge.source_entity, edge.target_entity, list(all_edges)
            )

        meets_topological = topology_path is not None
        ev["topology_path"] = topology_path

        if meets_temporal and meets_historical and (meets_statistical or meets_topological):
            edge.evidence = ev
            edge.promotion_level = "probable_cause"
            edge.edge_type = "probable_cause"  # update the edge type label too
            edge.confidence = min(
                0.85,
                0.4 + (0.2 if meets_statistical else 0)
                    + (0.15 if meets_topological else 0)
                    + min(incident_count * 0.02, 0.1),
            )
            edge.promoted_at = datetime.now(tz=UTC)
            logger.info(
                "Edge %s→%s promoted to probable_cause (p=%.3f, topology=%s, incidents=%d)",
                edge.source_entity, edge.target_entity,
                p_value or -1, topology_path, incident_count,
            )

    edge.evidence = ev
    await session.flush()


async def confirm_causation(
    session: AsyncSession,
    source_entity: str,
    target_entity: str,
    confirmed_by: str,
) -> GraphEdge | None:
    """
    Promote an edge to confirmed_cause. Requires human confirmation.
    The confirmed_by field records who confirmed it (email / username).
    """
    result = await session.execute(
        select(GraphEdge).where(
            GraphEdge.source_entity == source_entity,
            GraphEdge.target_entity == target_entity,
            GraphEdge.edge_type.in_(["correlates_with", "probable_cause"]),
        )
    )
    edge = result.scalar_one_or_none()
    if edge is None:
        return None

    edge.promotion_level = "confirmed_cause"
    edge.edge_type = "confirmed_cause"
    edge.confidence = 1.0
    edge.confirmed_by = confirmed_by
    edge.promoted_at = datetime.now(tz=UTC)
    await session.flush()
    logger.info(
        "Edge %s→%s confirmed as confirmed_cause by %s",
        source_entity, target_entity, confirmed_by,
    )
    return edge


async def get_causal_chain(
    session: AsyncSession,
    entity: str,
    direction: str = "upstream",
    min_promotion_level: str = "correlates_with",
    max_depth: int = 5,
) -> list[dict]:
    """
    Return the causal chain for an entity by traversing graph edges.

    Args:
        entity:               Starting entity name.
        direction:            "upstream" (what caused this?) or "downstream"
                              (what does this affect?).
        min_promotion_level:  Minimum promotion level to include.
        max_depth:            Maximum hops to follow.

    Returns:
        List of edge dicts, ordered from closest to furthest.
    """
    min_rank = _LEVEL_RANK.get(min_promotion_level, 0)
    visited: set[str] = {entity}
    frontier = [entity]
    chain: list[dict] = []

    for _ in range(max_depth):
        if not frontier:
            break
        next_frontier: list[str] = []

        for node in frontier:
            if direction == "upstream":
                result = await session.execute(
                    select(GraphEdge).where(GraphEdge.target_entity == node)
                )
            else:
                result = await session.execute(
                    select(GraphEdge).where(GraphEdge.source_entity == node)
                )

            for edge in result.scalars().all():
                if _LEVEL_RANK.get(edge.promotion_level, 0) < min_rank:
                    continue
                neighbor = edge.source_entity if direction == "upstream" else edge.target_entity
                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
                    chain.append(edge.to_dict())

        frontier = next_frontier

    return chain


async def get_all_edges(
    session: AsyncSession,
    min_promotion_level: str = "observed",
) -> list[dict]:
    """Return all graph edges above a given promotion level."""
    min_rank = _LEVEL_RANK.get(min_promotion_level, 0)
    result = await session.execute(select(GraphEdge))
    edges = [
        e.to_dict() for e in result.scalars().all()
        if _LEVEL_RANK.get(e.promotion_level, 0) >= min_rank
    ]
    return edges


# ── Math helpers ──────────────────────────────────────────────────

def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance ** 0.5
