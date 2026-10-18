"""
RootOps — Codebase Summary Model

Stores a persistent LLM-generated architectural summary per repository.
Injected into every LLM system prompt for that repo so the model has
pre-loaded architectural context without needing to retrieve it.

One row per repository. The SINGLETON_ID is kept for backward-compat
but the primary key is now repo_id.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Legacy fallback — used when no repo_id is available
SINGLETON_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class CodebaseSummary(Base):
    """LLM-generated architectural summary for one repository."""

    __tablename__ = "codebase_summary"

    # Primary key is the repo_id — one summary per repo
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    repo_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def is_valid(self) -> bool:
        return bool(self.summary and len(self.summary) > 50)
