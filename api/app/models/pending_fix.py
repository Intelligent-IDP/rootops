"""
RootOps — Pending Fix Model

Stores auto-heal diagnosis results in PostgreSQL so they survive restarts
and are accessible from any API instance.

Replaces the in-memory `_pending_fixes` dict in healer.py.
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PendingFix(Base):
    """A pending auto-heal diagnosis and suggested fix."""

    __tablename__ = "pending_fixes"

    # Short UUID prefix — human-readable, used as the public fix_id.
    fix_id: Mapped[str] = mapped_column(String(8), primary_key=True)

    error_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    error_service: Mapped[str] = mapped_column(String(256), nullable=False)
    file_reference: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    related_file: Mapped[str] = mapped_column(String(1024), nullable=False)
    related_lines: Mapped[str] = mapped_column(String(64), nullable=False)
    similarity_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    diagnosis: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_code: Mapped[str] = mapped_column(Text, nullable=False, default="")
    original_code: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ── Trust Ladder ─────────────────────────────────────────────
    # Composite confidence: similarity × context_quality × evidence_count (0–1)
    confidence_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Number of downstream services that could be affected by this fix.
    # Derived from the service_dependencies graph at diagnosis time.
    blast_radius: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # low | medium | high — derived from blast_radius and service criticality
    blast_radius_level: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # True when blast_radius_level is "high" OR confidence_score < threshold.
    # PR creation is blocked unless this is False.
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # True only when confidence >= HEAL_AUTO_APPLY_MIN_CONFIDENCE AND
    # blast_radius_level == "low". The CI gate still runs staging tests.
    auto_apply_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # LLM-generated rollback instructions generated alongside the fix.
    rollback_plan: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "fix_id": self.fix_id,
            "error_level": self.error_level,
            "error_message": self.error_message,
            "error_service": self.error_service,
            "file_reference": self.file_reference,
            "related_file": self.related_file,
            "related_lines": self.related_lines,
            "similarity_score": self.similarity_score,
            "diagnosis": self.diagnosis,
            "suggested_code": self.suggested_code,
            "original_code": self.original_code,
            "confidence_score": self.confidence_score,
            "blast_radius": self.blast_radius,
            "blast_radius_level": self.blast_radius_level,
            "requires_approval": self.requires_approval,
            "auto_apply_eligible": self.auto_apply_eligible,
            "rollback_plan": self.rollback_plan,
            "created_at": self.created_at.isoformat(),
        }
