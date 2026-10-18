"""
RootOps V2 — Code Chunk Model

The core unit of the Semantic Engine.  Each row represents a semantically
meaningful fragment of source code, stored alongside its vector embedding
for sub-millisecond cosine-similarity retrieval via pgvector.
"""

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.models.base import Base

settings = get_settings()


class CodeChunk(Base):
    """A semantically embedded fragment of source code."""

    __tablename__ = "code_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    file_path: Mapped[str] = mapped_column(
        String(1024), nullable=False, index=True
    )
    chunk_content: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── Semantic Embedding (float16 quantized) ─────────────────
    embedding = mapped_column(
        HALFVEC(settings.EMBEDDING_DIMENSION),
        nullable=True,
    )

    # ── Foreign Keys ─────────────────────────────────────────────
    repo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    commit_sha: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("commits.sha"),
        nullable=False,
        index=True,
    )

    # ── Timestamps ───────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # ── Relationships ────────────────────────────────────────────
    commit: Mapped["Commit"] = relationship(  # noqa: F821
        back_populates="code_chunks",
    )

    def __repr__(self) -> str:
        return (
            f"<CodeChunk {self.file_path}:"
            f"{self.start_line}-{self.end_line}>"
        )
