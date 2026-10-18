"""
RootOps — LogConcept Model

Stores pattern-level understanding of log streams rather than raw log lines.
10M log lines → ~1K LogConcepts → 1K embeddings.

Each LogConcept represents a recurring log pattern (template) with:
- Canonical message template: "Timeout connecting to Redis after <*>ms"
- Occurrence counts and temporal distribution (hourly histogram)
- Trend detection (rising / stable / falling)
- Correlations to deployments and other concepts
- ONE embedding vector (not per-line)
"""

from __future__ import annotations

from datetime import UTC, datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LogConcept(Base):
    """A pattern-level understanding extracted from a log stream."""

    __tablename__ = "log_concepts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ── Identity ──────────────────────────────────────────────────
    service_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    drain_cluster_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Severity / classification ──────────────────────────────────
    # Most common severity level seen in this pattern
    severity: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ── Occurrence counts ─────────────────────────────────────────
    total_occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # ── Temporal distribution ─────────────────────────────────────
    # JSON: {"YYYY-MM-DDTHH": count, ...} — last LOG_CONCEPT_HISTOGRAM_HOURS entries
    temporal_histogram: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # JSON: {"YYYY-MM-DD": count, ...} — last 7 days
    daily_counts: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # rising | falling | stable | unknown
    trend: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")

    # ── Correlations ──────────────────────────────────────────────
    # JSON: [{"type": "deployment"|"commit"|"concept", "id": "...", "lag_seconds": N}]
    correlations: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # ── Embedding ─────────────────────────────────────────────────
    # Single vector representing the entire pattern (not per log line)
    embedding: Mapped[list[float] | None] = mapped_column(
        HALFVEC(768), nullable=True
    )
    embedding_model_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="v1"
    )

    # ── Timestamps ───────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "service_name": self.service_name,
            "template": self.template,
            "drain_cluster_id": self.drain_cluster_id,
            "severity": self.severity,
            "total_occurrences": self.total_occurrences,
            "temporal_histogram": self.temporal_histogram,
            "daily_counts": self.daily_counts,
            "trend": self.trend,
            "correlations": self.correlations,
            "embedding_model_version": self.embedding_model_version,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
        }
