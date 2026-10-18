"""
RootOps — GraphEdge Model

Evidence-gated causal edges in the knowledge graph.

Edges are promoted through confidence levels based on accumulated evidence:
  observed → correlates_with → probable_cause → confirmed_cause

Promotion requires multiple signals (temporal, statistical, topological)
so the LLM cannot falsely assert causation from a single correlation.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class GraphEdge(Base):
    """A directional edge between two entities in the knowledge graph."""

    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint(
            "source_entity", "target_entity", "edge_type",
            name="uq_graph_edge_source_target_type",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True,
        default=lambda: str(_uuid.uuid4()),
    )

    # ── Entities ──────────────────────────────────────────────────
    source_entity: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entity: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Edge classification ───────────────────────────────────────
    # Base type: calls | publishes_to | reads_from | correlates_with |
    #            probable_cause | confirmed_cause | introduced | resolved
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # Promotion level within causal chain:
    # observed → correlates_with → probable_cause → confirmed_cause
    promotion_level: Mapped[str] = mapped_column(
        String(32), nullable=False, default="observed"
    )

    # ── Confidence ───────────────────────────────────────────────
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # ── Evidence ─────────────────────────────────────────────────
    # JSON blob storing the evidence that backs this edge:
    # {
    #   "temporal_precedence_ms": 340,    # A precedes B by this many ms on avg
    #   "granger_p_value": 0.02,          # Granger causality test p-value
    #   "topology_path": ["A", "B", "C"], # Graph path connecting entities
    #   "incident_count": 5,              # Times this pattern was observed
    #   "correlation_window_seconds": 300
    # }
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── Occurrence tracking ───────────────────────────────────────
    incident_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # ── Timestamps ───────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_by: Mapped[str | None] = mapped_column(String(256), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_entity": self.source_entity,
            "source_type": self.source_type,
            "target_entity": self.target_entity,
            "target_type": self.target_type,
            "edge_type": self.edge_type,
            "promotion_level": self.promotion_level,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "incident_count": self.incident_count,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "promoted_at": self.promoted_at.isoformat() if self.promoted_at else None,
            "confirmed_by": self.confirmed_by,
        }
