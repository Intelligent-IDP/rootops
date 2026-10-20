"""
RootOps — Ingestion API Router

Endpoints for triggering and monitoring repository ingestion.
Supports both local paths and remote URLs.

Multi-repo: each ingestion creates/updates a Repository row and uses
repo_id as the primary key for IngestionState (not a singleton).

Architecture: POST /api/ingest returns 202 immediately and runs the
ingestion in a background asyncio task.  This keeps the API container
responsive and — critically — alive even if the ingest task OOMs or
crashes. The client polls GET /api/ingest/status for progress.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session, get_db
from app.models.code_chunk import CodeChunk
from app.models.commit import Commit
from app.models.ingestion_state import IngestionState
from app.models.repository import LEGACY_REPO_ID, Repository
from app.services.git_ingestor import clone_and_ingest, ingest_repository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ingest", tags=["ingestion"])

# ── Legacy singleton used by auto-ingest path ─────────────────────
_LEGACY_REPO_ID = LEGACY_REPO_ID


# ── DB helpers ────────────────────────────────────────────────────

async def _get_state(session: AsyncSession, repo_id: uuid.UUID | None = None) -> dict:
    """Read ingestion state for a repo (or the legacy singleton)."""
    pk = repo_id or _LEGACY_REPO_ID
    row = await session.get(IngestionState, pk)
    if row is None:
        return {"state": "idle", "repo_path": None, "repo_url": None, "stats": None, "error": None}
    return row.to_dict()


async def _set_state(
    session: AsyncSession,
    repo_id: uuid.UUID | None = None,
    **fields,
) -> None:
    """Upsert the ingestion state row for a given repo."""
    pk = repo_id or _LEGACY_REPO_ID

    # Ensure the legacy placeholder repository exists before writing state
    if pk == _LEGACY_REPO_ID:
        existing_repo = await session.get(Repository, pk)
        if not existing_repo:
            session.add(Repository(
                id=pk,
                name="legacy",
                description="Auto-created placeholder for single-repo mode.",
            ))
            await session.flush()

    stmt = (
        pg_insert(IngestionState)
        .values(repo_id=pk, **fields)
        .on_conflict_do_update(
            index_elements=["repo_id"],
            set_={**fields},
        )
    )
    await session.execute(stmt)
    await session.commit()


# ── Request / Response models ─────────────────────────────────────

class IngestRequest(BaseModel):
    """Request body for repository ingestion."""
    repo_path: str | None = Field(
        default=None,
        description="Local filesystem path to the git repository.",
    )
    repo_url: str | None = Field(
        default=None,
        description="HTTPS URL of a git repository to clone and ingest.",
    )
    branch: str = Field(default="HEAD", description="Git branch or ref to ingest.")
    max_commits: int | None = Field(
        default=100,
        description="Maximum number of commits to ingest (None = all).",
    )

    # ── Repository metadata ───────────────────────────────────────
    name: str | None = Field(
        default=None,
        description=(
            "Short display name for this repository, e.g. 'payment-service'. "
            "Defaults to the last path component of repo_path/repo_url."
        ),
    )
    team: str | None = Field(
        default=None,
        description="Owning team, e.g. 'payments', 'platform'.",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Free-form tags, e.g. ['python', 'critical-path'].",
    )
    description: str | None = Field(
        default=None,
        description="Short description of what this service does.",
    )

    @model_validator(mode="after")
    def validate_source(self):
        if not self.repo_path and not self.repo_url:
            raise ValueError("Provide either 'repo_path' or 'repo_url'.")
        if self.repo_path and self.repo_url:
            raise ValueError("Provide only one of 'repo_path' or 'repo_url', not both.")
        return self


class IngestResponse(BaseModel):
    status: str
    message: str
    repo_id: str | None = None
    repo_name: str | None = None


class IngestStatusResponse(BaseModel):
    state: str
    repo_path: str | None = None
    repo_url: str | None = None
    stats: dict | None = None
    error: str | None = None


# ── Background ingestion task ─────────────────────────────────────

async def _run_ingest_in_background(
    *,
    repo_path: str | None,
    repo_url: str | None,
    branch: str,
    max_commits: int | None,
    repo_name: str | None,
    team: str | None,
    tags: list | None,
    description: str | None,
    repo_id: uuid.UUID | None,
) -> None:
    """Execute ingestion in its own DB session, fully detached from the
    original HTTP request.  Exceptions are caught and persisted as
    'failed' state — they never propagate to the API event loop."""
    try:
        async with async_session() as session:
            if repo_path:
                stats = await ingest_repository(
                    repo_path=repo_path,
                    session=session,
                    branch=branch,
                    max_commits=max_commits,
                    repo_name=repo_name,
                    team=team,
                    tags=tags,
                    description=description,
                )
            else:
                stats = await clone_and_ingest(
                    repo_url=repo_url,
                    session=session,
                    branch=branch,
                    max_commits=max_commits,
                    repo_name=repo_name,
                    team=team,
                    tags=tags,
                    description=description,
                )
            actual_repo_id = uuid.UUID(stats["repo_id"])
            await _set_state(session, actual_repo_id, state="completed", stats=stats)
            logger.info(
                "Background ingest complete: %d commits, %d chunks from %d files",
                stats.get("commits_ingested", 0),
                stats.get("chunks_ingested", 0),
                stats.get("files_processed", 0),
            )
    except Exception as exc:
        logger.exception("Background ingestion failed")
        try:
            async with async_session() as session:
                await _set_state(session, repo_id, state="failed", error=str(exc))
        except Exception:
            logger.exception("Could not persist failure state")


# ── Endpoints ─────────────────────────────────────────────────────

@router.post("", response_model=IngestResponse, status_code=202)
async def trigger_ingestion(
    request: IngestRequest,
    session: AsyncSession = Depends(get_db),
):
    """Trigger ingestion of a git repository.

    Returns 202 immediately and runs ingestion in the background.
    Poll GET /api/ingest/status for progress.
    """
    if request.repo_path:
        repo_path = Path(request.repo_path)
        if not repo_path.exists():
            raise HTTPException(400, f"Repository path does not exist: {request.repo_path}")
        if not (repo_path / ".git").exists():
            raise HTTPException(400, f"Not a git repository: {request.repo_path}")

        repo_name = request.name or repo_path.name
    else:
        repo_name = request.name or request.repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    # Check for already-running ingestion
    existing_repo = (
        await session.execute(select(Repository).where(Repository.name == repo_name))
    ).scalar_one_or_none()
    repo_id = existing_repo.id if existing_repo else None

    current = await _get_state(session, repo_id)
    if current["state"] == "running":
        raise HTTPException(409, "An ingestion is already in progress for this repository.")

    # Mark as running immediately so subsequent calls get a 409
    await _set_state(
        session, repo_id,
        state="running",
        repo_path=request.repo_path,
        repo_url=request.repo_url,
        stats=None, error=None,
    )

    # Fire off ingestion in a background asyncio task
    asyncio.create_task(
        _run_ingest_in_background(
            repo_path=request.repo_path,
            repo_url=request.repo_url,
            branch=request.branch,
            max_commits=request.max_commits,
            repo_name=request.name,
            team=request.team,
            tags=request.tags,
            description=request.description,
            repo_id=repo_id,
        )
    )

    return IngestResponse(
        status="accepted",
        repo_id=str(repo_id) if repo_id else None,
        repo_name=repo_name,
        message=(
            f"Ingestion of '{repo_name}' started in the background. "
            "Poll GET /api/ingest/status for progress."
        ),
    )


@router.get("/status", response_model=IngestStatusResponse)
async def get_ingestion_status(
    session: AsyncSession = Depends(get_db),
):
    """Check the current ingestion status (latest / global totals).

    Returns live totals from the database so the dashboard reflects the
    true platform state even after de-duplicated re-ingestion runs.
    """
    total_commits = (await session.execute(select(func.count(Commit.id)))).scalar() or 0
    total_chunks  = (await session.execute(select(func.count(CodeChunk.id)))).scalar() or 0
    indexed_files = (
        await session.execute(
            select(func.count()).select_from(
                select(CodeChunk.repo_id, CodeChunk.file_path)
                .where(CodeChunk.repo_id != LEGACY_REPO_ID)
                .distinct()
                .subquery()
            )
        )
    ).scalar() or 0

    # Find the most recently active ingestion state
    latest = (
        await session.execute(
            select(IngestionState).order_by(IngestionState.updated_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    if latest:
        current = latest.to_dict()
    else:
        current = {"state": "idle", "repo_path": None, "repo_url": None, "stats": None, "error": None}

    last_stats = current.get("stats") or {}
    merged_stats = {
        "commits_ingested": total_commits,
        "chunks_ingested":  total_chunks,
        "files_indexed": indexed_files,
        "files_processed":  last_stats.get("files_processed", 0),
    }

    return IngestStatusResponse(
        state=current["state"],
        repo_path=current.get("repo_path"),
        repo_url=current.get("repo_url"),
        stats=merged_stats,
        error=current.get("error"),
    )


@router.post("/reset")
async def reset_ingestion_state(
    session: AsyncSession = Depends(get_db),
):
    """Reset stale ingestion states so the dashboard no longer shows 'failed'.

    Marks any 'failed' or 'running' ingestion states as 'completed' if
    the repository already has chunks ingested, or deletes them otherwise.
    """
    stale = (
        await session.execute(
            select(IngestionState).where(
                IngestionState.state.in_(["failed", "running"])
            )
        )
    ).scalars().all()

    reset_count = 0
    for st in stale:
        chunk_count = (
            await session.execute(
                select(func.count(CodeChunk.id)).where(
                    CodeChunk.repo_id == st.repo_id
                )
            )
        ).scalar() or 0
        if chunk_count > 0:
            st.state = "completed"
            st.error = None
        else:
            await session.delete(st)
        reset_count += 1

    await session.commit()
    return {"reset": reset_count, "message": f"Reset {reset_count} stale ingestion state(s)."}
