"""
RootOps — Repository Model

A registered repository is the top-level unit of the multi-repo Digital Twin.
Every code chunk, commit, log entry, and codebase summary belongs to exactly
one repository. Queries can be scoped to one repo, a team, or the entire org.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

LEGACY_REPO_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")


class Repository(Base):
    """A registered git repository in the platform."""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Identity ─────────────────────────────────────────────────
    name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
        doc="Short display name, e.g. 'payment-service'.",
    )
    url: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        doc="HTTPS clone URL, e.g. 'https://github.com/org/payment-service'.",
    )
    local_path: Mapped[str | None] = mapped_column(
        String(1024), nullable=True,
        doc="Local filesystem path (if ingested from disk).",
    )

    # ── Org structure ─────────────────────────────────────────────
    team: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True,
        doc="Owning team, e.g. 'payments', 'platform', 'fraud'.",
    )
    tags: Mapped[list | None] = mapped_column(
        JSON, nullable=True,
        doc="Free-form tags, e.g. ['python', 'critical-path', 'external-api'].",
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        doc="Short description of what this service does.",
    )

    # ── Ingestion state ───────────────────────────────────────────
    last_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    commit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Timestamps ────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "url": self.url,
            "local_path": self.local_path,
            "team": self.team,
            "tags": self.tags or [],
            "description": self.description,
            "last_ingested_at": self.last_ingested_at.isoformat() if self.last_ingested_at else None,
            "chunk_count": self.chunk_count,
            "commit_count": self.commit_count,
        }

    def __repr__(self) -> str:
        return f"<Repository {self.name}>"
