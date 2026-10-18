"""
RootOps V2 — Log Entry Model

Stores semantically embedded application log entries alongside their
metadata. Enables cross-correlation between production errors and the
code that produced them — the foundation of the Semantic Engine's
operational intelligence.
"""

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.config import get_settings
from app.models.base import Base

settings = get_settings()


class LogEntry(Base):
    """An embedded application log entry with extracted metadata."""

    __tablename__ = "log_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    # ── Repository ───────────────────────────────────────────────
    repo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Source Identification ────────────────────────────────────
    source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="raw",
        doc="Origin of the log: 'raw', 'otel', 'file'",
    )
    service_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True,
        doc="Name of the service that produced this log.",
    )

    # ── Log Content ─────────────────────────────────────────────
    timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
        doc="Parsed timestamp from the log line.",
    )
    level: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True,
        doc="Log level: ERROR, WARN, INFO, DEBUG.",
    )
    message: Mapped[str] = mapped_column(
        Text, nullable=False,
        doc="Full log message text.",
    )
    parsed_error: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        doc="Extracted exception class / stack trace summary.",
    )

    # ── Code Cross-References ───────────────────────────────────
    file_reference: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, index=True,
        doc="File path extracted from stack trace (e.g. 'payment.py').",
    )
    line_reference: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        doc="Line number extracted from stack trace.",
    )

    # ── Semantic Embedding (float16 quantized) ──────────────────
    embedding = mapped_column(
        HALFVEC(settings.EMBEDDING_DIMENSION),
        nullable=True,
    )

    # ── Extra Metadata ──────────────────────────────────────────
    metadata_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
        doc="Extra fields: request_id, log_group, trace_id, etc.",
    )

    # ── Timestamps ──────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<LogEntry [{self.level}] {self.service_name}: "
            f"{self.message[:60]}>"
        )
