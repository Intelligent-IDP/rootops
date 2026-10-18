"""
RootOps — Service Dependency Model

Stores statically-extracted service-to-service dependencies discovered
during ingestion. Each row represents one observed call/event/reference
from one repo to another.

The dependency graph built from these rows powers:
  - Dashboard: interactive topology view
  - Flow tracing: "trace a payment from API entry to ledger write"
  - Cross-repo PR impact: "this change breaks callers in N other services"

Dependency types extracted:
  http      — requests.get/post, httpx, fetch, axios to another service URL
  event     — kafka producer.send, pubsub publish, SQS send_message
  grpc      — grpc channel/stub calls
  import    — direct Python/Go/JS imports of another service's shared library
  env_ref   — environment variable references to another service (SERVICE_URL=...)
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

DEPENDENCY_TYPES = ("http", "event", "grpc", "import", "env_ref")


class ServiceDependency(Base):
    """A detected dependency from one repository to another."""

    __tablename__ = "service_dependencies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ── Graph edge ────────────────────────────────────────────────
    source_repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True,
    )
    source_repo_name: Mapped[str] = mapped_column(String(255), nullable=False)

    target_repo_name: Mapped[str] = mapped_column(
        String(255), nullable=False,
        doc="Name of the target service (may not be ingested yet).",
    )
    # Nullable — target may not be registered
    target_repo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )

    # ── Evidence ──────────────────────────────────────────────────
    dependency_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
        doc="One of: http, event, grpc, import, env_ref",
    )
    source_file: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_pattern: Mapped[str] = mapped_column(
        Text, nullable=False,
        doc="The matched line/snippet that revealed this dependency.",
    )

    # ── Weight ────────────────────────────────────────────────────
    # How many distinct call sites were found — used to weight graph edges.
    call_count: Mapped[int] = mapped_column(
        default=1,
        doc="Number of distinct call sites for this source→target pair.",
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0,
        doc="Extraction confidence 0–1. Regex matches = 1.0.",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        # One row per (source_repo, target_name, type, file) — deduplicated
        Index(
            "uq_dep_source_target_type_file",
            "source_repo_id", "target_repo_name", "dependency_type", "source_file",
            unique=True,
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "source_repo_id": str(self.source_repo_id),
            "source_repo_name": self.source_repo_name,
            "target_repo_name": self.target_repo_name,
            "target_repo_id": str(self.target_repo_id) if self.target_repo_id else None,
            "dependency_type": self.dependency_type,
            "source_file": self.source_file,
            "source_pattern": self.source_pattern,
            "call_count": self.call_count,
            "confidence": self.confidence,
        }
