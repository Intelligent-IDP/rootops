"""Latest detailed Breadth flowgraph imported for a repository."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BreadthGraphSnapshot(Base):
    """Persist Breadth's full node hierarchy and typed edges without roll-up."""

    __tablename__ = "breadth_graph_snapshots"

    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    flowgraph: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
