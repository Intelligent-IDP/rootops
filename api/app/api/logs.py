"""
RootOps — Log Ingestion API Router

Endpoints for feeding application logs into the Semantic Engine.
Logs are parsed, embedded, and stored alongside code chunks for
cross-correlation during queries.

Supports:
  • Raw log text ingestion (plain text or JSON lines)
  • OpenTelemetry OTLP/HTTP log receiver (POST /v1/logs)
  • Aggregate log statistics
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.log_entry import LogEntry
from app.services.log_ingestor import ingest_logs
from app.services.otel_collector import (
    get_otel_receiver_stats,
)

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api/ingest/logs", tags=["logs"])


# ── Pydantic Models ──────────────────────────────────────────────

class LogIngestRequest(BaseModel):
    """Request body for log ingestion."""
    raw_text: str = Field(
        ...,
        description="Raw log text (plain text or JSON lines).",
        examples=[
            (
                "2024-03-01 12:00:00 ERROR payment.py:47 "
                "NullPointerException: amount was None"
            )
        ],
    )
    service_name: str = Field(
        ...,
        description="Name of the service that produced these logs.",
        examples=["payment-service"],
    )
    source: str = Field(
        default="raw",
        description="Source type: 'raw', 'otel', 'file'.",
    )


class LogIngestResponse(BaseModel):
    """Response from log ingestion."""
    status: str
    entries_ingested: int
    dropped: int = 0
    drop_reasons: dict[str, int] = {}
    by_level: dict[str, int]
    service_name: str


class LogStatsResponse(BaseModel):
    """Aggregate stats for ingested logs."""
    total_entries: int
    by_level: dict[str, int]
    by_service: dict[str, int]


class OtelReceiverStatusResponse(BaseModel):
    """Status of the OTEL log receiver."""
    enabled: bool
    total_requests_received: int
    total_log_records_received: int
    total_entries_ingested: int
    total_dropped: int
    last_received_at: str | None
    services_seen: dict[str, int]


# ── Raw Log Ingestion ────────────────────────────────────────────

@router.post("", response_model=LogIngestResponse)
async def trigger_log_ingestion(
    request: LogIngestRequest,
    session: AsyncSession = Depends(get_db),
):
    """Ingest application logs into the Semantic Engine.

    Accepts raw log text (plain text or JSON lines), parses it into
    structured entries, embeds each entry, and stores them for
    cross-correlation with code chunks during queries.
    """
    try:
        stats = await ingest_logs(
            raw_text=request.raw_text,
            service_name=request.service_name,
            session=session,
            source=request.source,
        )
        return LogIngestResponse(
            status="completed",
            entries_ingested=stats["entries_ingested"],
            dropped=stats.get("dropped", 0),
            drop_reasons=stats.get("drop_reasons", {}),
            by_level=stats["by_level"],
            service_name=stats["service_name"],
        )
    except Exception as e:
        logger.exception("Log ingestion failed")
        raise HTTPException(
            status_code=500,
            detail=f"Log ingestion failed: {e}",
        )


# ── Aggregate Stats ──────────────────────────────────────────────

@router.get("/stats", response_model=LogStatsResponse)
async def get_log_stats(
    session: AsyncSession = Depends(get_db),
):
    """Get aggregate statistics for ingested log entries."""
    # Total count
    total_result = await session.execute(
        select(func.count(LogEntry.id))
    )
    total = total_result.scalar() or 0

    # Count by level
    level_result = await session.execute(
        select(LogEntry.level, func.count(LogEntry.id))
        .group_by(LogEntry.level)
    )
    by_level = {row[0] or "UNKNOWN": row[1] for row in level_result.fetchall()}

    # Count by service
    service_result = await session.execute(
        select(LogEntry.service_name, func.count(LogEntry.id))
        .group_by(LogEntry.service_name)
    )
    by_service = {row[0]: row[1] for row in service_result.fetchall()}

    return LogStatsResponse(
        total_entries=total,
        by_level=by_level,
        by_service=by_service,
    )


# ── OpenTelemetry Receiver Status ────────────────────────────────

@router.get("/otel/status", response_model=OtelReceiverStatusResponse)
async def otel_receiver_status():
    """Get the current status and stats of the OTEL log receiver."""
    return OtelReceiverStatusResponse(**get_otel_receiver_stats())
