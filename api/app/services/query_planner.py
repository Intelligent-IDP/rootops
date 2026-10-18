"""
RootOps — Query Planner

Classifies incoming natural language queries and produces a typed retrieval
plan that the RAG engine executes in parallel across the three layers:

  Layer 1: Vector Space   — code chunks, log concepts, doc sections
  Layer 2: LogConcepts    — pattern-level temporal understanding
  Layer 3: Knowledge Graph — relational traversal (entities, edges)

Query types and their retrieval strategies:
  diagnostic   — deep graph (causes) + log concepts + code
  exploratory  — code + docs, shallow graph
  impact       — graph traversal (downstream deps) + code
  ownership    — graph (developer/team nodes) + code
  risk         — code + graph (patterns) + log concepts
  historical   — log concepts with time window + commits
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Query type enum ───────────────────────────────────────────────

class QueryType(str, Enum):
    DIAGNOSTIC = "diagnostic"
    EXPLORATORY = "exploratory"
    IMPACT = "impact"
    OWNERSHIP = "ownership"
    RISK = "risk"
    HISTORICAL = "historical"
    GENERAL = "general"


# ── Retrieval plan ────────────────────────────────────────────────

@dataclass
class RetrievalPlan:
    query_type: QueryType
    target_entities: list[str] = field(default_factory=list)

    # Layer 1: Vector retrieval switches
    use_code_retrieval: bool = True
    use_log_retrieval: bool = True
    use_concept_retrieval: bool = False  # LogConcepts (aggregated)

    # Layer 3: Graph traversal
    use_graph_traversal: bool = False
    graph_depth: int = 1
    graph_direction: str = "both"  # upstream | downstream | both

    # Retrieval sizes
    code_fetch_k: int = 20
    log_fetch_k: int = 10
    concept_fetch_k: int = 10

    # Optional time scoping (ISO strings)
    time_from: str | None = None
    time_to: str | None = None

    # Minimum similarity threshold override (None = use global config)
    similarity_threshold_override: float | None = None


# ── Signal patterns for classification ───────────────────────────

_DIAGNOSTIC_SIGNALS = re.compile(
    r"\b(why|cause|reason|root.cause|slow|latency|error|fail|crash|broken|"
    r"timeout|exception|bug|issue|problem|debug|diagnos)\b",
    re.IGNORECASE,
)
_EXPLORATORY_SIGNALS = re.compile(
    r"\b(how.does|explain|what.is|describe|overview|show.me|walk.me|"
    r"understand|architecture|flow|works?)\b",
    re.IGNORECASE,
)
_IMPACT_SIGNALS = re.compile(
    r"\b(if|what.if|impact|affect|depend|downstream|upstream|cascade|"
    r"break|remove|delete|migrate)\b",
    re.IGNORECASE,
)
_OWNERSHIP_SIGNALS = re.compile(
    r"\b(who|owner|owns|responsible|team|author|wrote|maintains?|contact)\b",
    re.IGNORECASE,
)
_RISK_SIGNALS = re.compile(
    r"\b(safe|risk|danger|vulnerable|security|pr|pull.request|review|"
    r"deploy|change|merge|regression)\b",
    re.IGNORECASE,
)
_HISTORICAL_SIGNALS = re.compile(
    r"\b(when|history|last.week|yesterday|monday|incident|outage|previously|"
    r"used.to|before|ago|changed|introduced)\b",
    re.IGNORECASE,
)


def classify_query(question: str) -> QueryType:
    """
    Score the question against signal patterns and return the winning QueryType.
    Falls back to GENERAL when signals are ambiguous.
    """
    matches = {
        QueryType.DIAGNOSTIC: len(_DIAGNOSTIC_SIGNALS.findall(question)),
        QueryType.EXPLORATORY: len(_EXPLORATORY_SIGNALS.findall(question)),
        QueryType.IMPACT: len(_IMPACT_SIGNALS.findall(question)),
        QueryType.OWNERSHIP: len(_OWNERSHIP_SIGNALS.findall(question)),
        QueryType.RISK: len(_RISK_SIGNALS.findall(question)),
        QueryType.HISTORICAL: len(_HISTORICAL_SIGNALS.findall(question)),
    }

    best_type = max(matches, key=lambda t: matches[t])
    best_score = matches[best_type]

    if best_score == 0:
        return QueryType.GENERAL

    logger.debug("Query classified as %s (score=%d): %s", best_type, best_score, question[:80])
    return best_type


def _extract_entities(question: str) -> list[str]:
    """
    Heuristically extract entity names from the question.
    Looks for capitalized words, quoted strings, and service-name patterns.
    """
    entities = []

    # Quoted names: 'payment-service', "auth"
    quoted = re.findall(r"['\"]([^'\"]{2,64})['\"]", question)
    entities.extend(quoted)

    # CamelCase or kebab-case identifiers (likely service/function names)
    identifiers = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+|[a-z]+-[a-z]+(?:-[a-z]+)*)\b", question)
    entities.extend(identifiers)

    return list(dict.fromkeys(entities))  # deduplicate preserving order


def plan_retrieval(question: str, query_type: QueryType | None = None) -> RetrievalPlan:
    """
    Build a RetrievalPlan for the given question.

    Args:
        question:   Natural language query.
        query_type: Override classification (useful for tests / explicit routing).

    Returns:
        A RetrievalPlan describing which layers to search and with what params.
    """
    if query_type is None:
        query_type = classify_query(question)

    entities = _extract_entities(question)
    max_depth = settings.QUERY_PLANNER_MAX_GRAPH_DEPTH

    plan = RetrievalPlan(query_type=query_type, target_entities=entities)

    if query_type == QueryType.DIAGNOSTIC:
        # Deep graph to find root causes; log concepts critical
        plan.use_graph_traversal = True
        plan.graph_depth = max_depth
        plan.graph_direction = "upstream"
        plan.use_concept_retrieval = True
        plan.code_fetch_k = 20
        plan.log_fetch_k = 15
        plan.concept_fetch_k = 15

    elif query_type == QueryType.EXPLORATORY:
        # Broad code retrieval, shallow graph for topology context
        plan.use_graph_traversal = True
        plan.graph_depth = 1
        plan.graph_direction = "both"
        plan.use_log_retrieval = False  # not needed for "how does X work"
        plan.code_fetch_k = 30

    elif query_type == QueryType.IMPACT:
        # Downstream graph traversal + code
        plan.use_graph_traversal = True
        plan.graph_depth = max_depth
        plan.graph_direction = "downstream"
        plan.use_concept_retrieval = True
        plan.code_fetch_k = 15

    elif query_type == QueryType.OWNERSHIP:
        # Shallow graph (people/team nodes) + code (authorship context)
        plan.use_graph_traversal = True
        plan.graph_depth = 1
        plan.graph_direction = "both"
        plan.use_log_retrieval = False
        plan.code_fetch_k = 10

    elif query_type == QueryType.RISK:
        # Code + log concepts for recent error patterns
        plan.use_concept_retrieval = True
        plan.use_graph_traversal = True
        plan.graph_depth = 2
        plan.code_fetch_k = 25
        plan.log_fetch_k = 10

    elif query_type == QueryType.HISTORICAL:
        # Log concepts with recency bias + commit history
        plan.use_concept_retrieval = True
        plan.use_log_retrieval = True
        plan.use_code_retrieval = True
        plan.concept_fetch_k = 20
        plan.log_fetch_k = 20
        plan.similarity_threshold_override = 0.2  # looser threshold for historical

    else:  # GENERAL
        plan.code_fetch_k = 20
        plan.log_fetch_k = 10

    return plan


def plan_to_metadata(plan: RetrievalPlan) -> dict:
    """Serialize the plan for inclusion in query response metadata."""
    return {
        "query_type": plan.query_type.value,
        "target_entities": plan.target_entities,
        "layers": {
            "code": plan.use_code_retrieval,
            "logs": plan.use_log_retrieval,
            "concepts": plan.use_concept_retrieval,
            "graph": plan.use_graph_traversal,
        },
        "graph_depth": plan.graph_depth if plan.use_graph_traversal else 0,
        "graph_direction": plan.graph_direction,
    }
