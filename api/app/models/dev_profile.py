"""
RootOps V2 — Developer Profile Model

Stores per-developer "fingerprints" — aggregated coding style patterns,
common idioms, and behavioural signatures extracted from their commit
history. Enables Developer Pattern Cloning ("Residency").
"""

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.models.base import Base

settings = get_settings()


class DevProfile(Base):
    """A developer's coding style fingerprint."""

    __tablename__ = "dev_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Identity ────────────────────────────────────────────────
    author_name: Mapped[str] = mapped_column(
        String(255), nullable=False,
    )
    author_email: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True,
    )

    # ── Style Profile ───────────────────────────────────────────
    pattern_summary: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        doc="LLM-generated natural language summary of coding style.",
    )
    code_patterns: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
        doc=(
            "Structured coding patterns: naming conventions, "
            "error handling style, preferred patterns, anti-patterns."
        ),
    )

    # ── Activity Stats ──────────────────────────────────────────
    commit_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    files_touched: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
        doc="Map of file paths to touch count.",
    )
    primary_languages: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
        doc="Language distribution, e.g. {'python': 45, 'js': 12}.",
    )

    # ── Semantic Embedding — style fingerprint (float16 quantized) ──
    embedding = mapped_column(
        HALFVEC(settings.EMBEDDING_DIMENSION),
        nullable=True,
        doc="Embedded representation of the developer's coding style.",
    )

    # ── Timestamps ──────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    last_updated: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<DevProfile {self.author_name} ({self.author_email})>"
