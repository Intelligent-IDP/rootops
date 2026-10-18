"""
RootOps — Ingestion State Model

One row per repository, tracking its current ingestion status.
Persisted in PostgreSQL so state survives restarts and is consistent
across multiple API instances.

Previously used a singleton UUID. Now keyed by repo_id (UUID of the
Repository row), which enables concurrent ingestion of multiple repos.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Legacy singleton — used for backward compatibility with single-repo mode
SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class IngestionState(Base):
    """Per-repository ingestion status."""

    __tablename__ = "ingestion_state"

    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def to_dict(self) -> dict:
        return {
            "repo_id": str(self.repo_id),
            "state": self.state,
            "repo_path": self.repo_path,
            "repo_url": self.repo_url,
            "stats": self.stats,
            "error": self.error,
        }
