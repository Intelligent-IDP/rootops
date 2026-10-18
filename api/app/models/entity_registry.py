"""
RootOps — EntityRegistry Model

Canonical entity registry for the knowledge graph.

Solves the cross-repo entity resolution problem: the same service may appear
as "payments", "payment-service", or "pay-api" across different repositories.
The registry maintains a stable UUID per entity with a list of aliases so the
graph never treats them as separate nodes.

Entities are never deleted — deprecated=True means the entity is gone but its
history (edges, incidents) is preserved.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EntityRegistry(Base):
    """Canonical entity with stable ID and alias list for cross-repo resolution."""

    __tablename__ = "entity_registry"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True,
        default=lambda: str(_uuid.uuid4()),
    )

    # ── Identity ──────────────────────────────────────────────────
    # The authoritative name used in the graph and displayed in the UI.
    canonical_name: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True, index=True
    )

    # Entity category from ontology:
    # Service | Function | Database | Cache | Queue | Topic | API |
    # Developer | Team | Commit | Deployment | Incident | LogConcept
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # ── Aliases ───────────────────────────────────────────────────
    # JSON: ["payments", "payment-service", "pay-api"]
    # All names that resolve to this entity across repos.
    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # ── Repository membership ─────────────────────────────────────
    # JSON: ["uuid1", "uuid2"] — repos where this entity has been observed
    repos: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Metadata ──────────────────────────────────────────────────
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Arbitrary extra data: {"team": "payments", "oncall": "pagerduty-id", ...}
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "canonical_name": self.canonical_name,
            "entity_type": self.entity_type,
            "aliases": self.aliases or [],
            "repos": self.repos or [],
            "deprecated": self.deprecated,
            "deprecated_at": self.deprecated_at.isoformat() if self.deprecated_at else None,
            "description": self.description,
            "metadata_json": self.metadata_json,
            "created_at": self.created_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
        }

    def matches(self, name: str) -> bool:
        """Return True if `name` matches this entity's canonical name or any alias."""
        name_lower = name.lower().strip()
        if self.canonical_name.lower() == name_lower:
            return True
        return name_lower in [a.lower() for a in (self.aliases or [])]
