"""
RootOps — FastAPI Application Entry Point

Provides the API surface and orchestrates startup/shutdown lifecycle events.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.concepts import router as concepts_router
from app.api.graph import router as graph_router
from app.api.heal import router as heal_router
from app.api.ingest import router as ingest_router
from app.api.logs import router as logs_router
from app.api.pr_review import router as pr_review_router
from app.api.profiles import router as profiles_router
from app.api.query import router as query_router
from app.api.repos import router as repos_router
from app.config import get_settings
from app.db import async_session, init_db
from app.services.git_ingestor import ingest_repository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

settings = get_settings()

__version__ = "1.0.0"


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Run startup tasks before the app begins serving requests."""
    # Ensure tables exist (Alembic is preferred for production)
    await init_db()

    # ── Log auto-detected memory tier ──────────────────────────────
    from app.config import _RAM_TIER_LABEL, DETECTED_RAM_GB
    backend = settings.LLM_BACKEND.lower()
    if settings.LLM_AVAILABLE:
        llm_status = f"{backend} ✅"
    elif backend in ("openai", "anthropic"):
        llm_status = f"{backend} ❌ (no API key — set {backend.upper()}_API_KEY)"
    else:
        llm_status = f"{backend} ❌"
    logger.info(
        "System RAM detected: %.1f GB → tier: %s  "
        "(batch=%d, workers=%d, encode_bs=%d, max_chars=%d, max_seq=%d, "
        "skip_summary=%s, skip_deps=%s, hyde=%s, llm=%s)",
        DETECTED_RAM_GB, _RAM_TIER_LABEL,
        settings.EMBED_BATCH_SIZE, settings.EMBED_WORKERS,
        settings.EMBED_ENCODE_BATCH_SIZE, settings.EMBED_MAX_CHARS,
        settings.EMBED_MAX_SEQ_LENGTH,
        settings.DISABLE_CODEBASE_SUMMARY, settings.DISABLE_DEP_EXTRACTION,
        settings.HYDE_ENABLED, llm_status,
    )

    # ── Reset orphaned ingestion states ────────────────────────────
    # If the API crashed or restarted mid-ingest (e.g. OOM kill), any
    # rows stuck in 'running' state will never complete.  Mark them as
    # failed so the UI doesn't show a permanent "Ingesting…" spinner.
    try:
        from sqlalchemy import update

        from app.models.ingestion_state import IngestionState

        async with async_session() as session:
            # Reset 'running' states to 'failed'
            result = await session.execute(
                update(IngestionState)
                .where(IngestionState.state == "running")
                .values(
                    state="failed",
                    error="Interrupted — the server restarted while ingestion was in progress.",
                )
            )
            if result.rowcount:
                logger.warning(
                    "Reset %d orphaned ingestion state(s) from 'running' → 'failed'",
                    result.rowcount,
                )
            # Reset stale 'failed' states back to 'idle' so the UI doesn't
            # permanently show a failure badge from a previous run.
            result2 = await session.execute(
                update(IngestionState)
                .where(IngestionState.state == "failed")
                .values(state="idle", error=None)
            )
            if result2.rowcount:
                logger.info(
                    "Cleared %d stale 'failed' ingestion state(s) → 'idle'",
                    result2.rowcount,
                )
            await session.commit()
    except Exception:
        logger.warning("Could not reset orphaned ingestion states", exc_info=True)

    # ── Validate embedding model dimension matches DB schema ───────
    # A mismatch (e.g. swapping all-MiniLM-L6-v2 for BGE-M3) silently
    # corrupts vector queries. Fail fast here rather than at query time.
    from app.services.embedding import validate_embedding_dimension
    try:
        validate_embedding_dimension(
            settings.EMBEDDING_MODEL_NAME,
            settings.EMBEDDING_DIMENSION,
        )
    except RuntimeError as exc:
        logger.critical("STARTUP ABORTED — %s", exc)
        raise SystemExit(1) from exc

    # ── Optional: Auto-ingest a repo on startup ─────────────────
    if settings.AUTO_INGEST_REPO_PATH:
        from pathlib import Path

        from app.api.ingest import _set_state

        repo = Path(settings.AUTO_INGEST_REPO_PATH)
        if repo.exists() and (repo / ".git").exists():
            logger.info("Auto-ingesting repo: %s", settings.AUTO_INGEST_REPO_PATH)
            try:
                async with async_session() as session:
                    await _set_state(
                        session,
                        None,  # repo_id resolved after ingest
                        state="running",
                        repo_path=settings.AUTO_INGEST_REPO_PATH,
                        repo_url=None,
                        stats=None,
                        error=None,
                    )
                    stats = await ingest_repository(
                        repo_path=settings.AUTO_INGEST_REPO_PATH,
                        session=session,
                        branch=settings.AUTO_INGEST_BRANCH,
                        max_commits=settings.AUTO_INGEST_MAX_COMMITS,
                    )
                    import uuid as _uuid
                    actual_repo_id = _uuid.UUID(stats["repo_id"])
                    await _set_state(session, actual_repo_id, state="completed", stats=stats)
                logger.info(
                    "Auto-ingest complete: %d commits, %d chunks from %d files",
                    stats.get("commits_ingested", 0),
                    stats.get("chunks_ingested", 0),
                    stats.get("files_processed", 0),
                )
            except Exception:
                logger.exception("Auto-ingest failed")
                async with async_session() as session:
                    await _set_state(session, None, state="failed", error="Auto-ingest failed on startup")
        else:
            logger.warning("AUTO_INGEST_REPO_PATH set but path not found: %s", repo)

    # ── OpenTelemetry log receiver ─────────────────────────────────
    if settings.OTEL_LOGS_RECEIVER_ENABLED:
        logger.info(
            "OTEL log receiver enabled — accepting OTLP/HTTP at POST /v1/logs"
        )
    else:
        logger.info("OTEL log receiver disabled (set OTEL_LOGS_RECEIVER_ENABLED=true to enable)")

    try:
        yield
    finally:
        pass


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "The AI-Native Internal Developer Platform. "
        "RootOps builds a persistent, semantic understanding of your "
        "entire codebase to govern software development from inception."
    ),
    version=__version__,
    lifespan=lifespan,
)

# ── Middleware ───────────────────────────────────────────────────
# Parse the comma-separated CORS_ALLOWED_ORIGINS list.
# Wildcard "*" is kept for convenience only when credentials are NOT needed.
_cors_origins = [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
_wildcard_only = _cors_origins == ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Credentials (cookies, Authorization header) are incompatible with "*".
    allow_credentials=not _wildcard_only,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routers ──────────────────────────────────────────────────
app.include_router(ingest_router)
app.include_router(logs_router)
app.include_router(query_router)
app.include_router(profiles_router)
app.include_router(heal_router)
app.include_router(repos_router)
app.include_router(concepts_router)
app.include_router(graph_router)
app.include_router(pr_review_router)


# ── OTLP/HTTP Log Receiver (standard endpoint) ──────────────────
# OpenTelemetry SDKs and Collectors export logs to POST /v1/logs.
# This endpoint is at the root level to comply with the OTLP spec.

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse as _JSONResponse

from app.db import get_db as _get_db
from app.services.otel_collector import ingest_otel_logs


@app.post("/v1/logs", tags=["otel"])
async def otlp_logs_receiver(
    request: Request,
    session=Depends(_get_db),
):
    """OTLP/HTTP log receiver — accepts ExportLogsServiceRequest (JSON).

    Point your OpenTelemetry SDK or Collector's OTLP/HTTP exporter at:
        http://<rootops-host>:8000/v1/logs

    Example Collector config:
        exporters:
          otlphttp:
            endpoint: http://rootops-api:8000
            tls:
              insecure: true
    """
    if not settings.OTEL_LOGS_RECEIVER_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="OTEL log receiver is disabled. Set OTEL_LOGS_RECEIVER_ENABLED=true.",
        )

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    try:
        await ingest_otel_logs(session, payload)
        # OTLP spec: return empty JSON object on success
        return {}
    except Exception as e:
        logger.exception("OTLP log ingestion failed")
        raise HTTPException(status_code=500, detail=f"OTLP log ingestion failed: {e}")


# ── Routes ───────────────────────────────────────────────────────
@app.get("/health", tags=["ops"])
async def health_check():
    """Readiness probe — confirms the API is alive."""
    return {
        "status": "ok",
        "ok": True,
        "app": settings.APP_NAME,
        "version": __version__,
        "llm_backend": settings.LLM_BACKEND,
    }


@app.get("/api/health/detailed", tags=["ops"])
async def health_detailed(session=Depends(_get_db)):
    """Deep health check — verifies every subsystem the API depends on.

    Returns individual status for: database, embedding model, LLM backend.
    Use this endpoint to power the Settings → System Status panel.
    HTTP 200 = all checks passed. HTTP 503 = at least one check failed.
    """
    import httpx as _httpx

    checks: dict[str, dict] = {}
    all_ok = True

    # ── 1. Database ───────────────────────────────────────────────
    try:
        from sqlalchemy import text as _text
        await session.execute(_text("SELECT 1"))
        checks["database"] = {"ok": True, "status": "connected"}
    except Exception as exc:
        checks["database"] = {"ok": False, "status": f"error: {exc}"}
        all_ok = False

    # ── 2. Embedding model ────────────────────────────────────────
    from app.services.embedding import _embed_cache
    model_loaded = bool(_embed_cache)
    checks["embedding_model"] = {
        "ok": True,          # model loads on first query — not a hard failure
        "model": settings.EMBEDDING_MODEL_NAME,
        "status": "loaded" if model_loaded else "will_load_on_first_query",
        "dimension": settings.EMBEDDING_DIMENSION,
    }

    # ── 3. LLM backend ────────────────────────────────────────────
    backend = settings.LLM_BACKEND.lower()
    if backend == "ollama":
        try:
            async with _httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            ollama_ok = r.status_code == 200
            try:
                models = [m["name"] for m in r.json().get("models", [])]
            except Exception:
                models = []
            checks["llm"] = {
                "ok": ollama_ok,
                "backend": "ollama",
                "url": settings.OLLAMA_BASE_URL,
                "model": settings.OLLAMA_MODEL,
                "status": "reachable" if ollama_ok else "unreachable",
                "available_models": models,
            }
            if not ollama_ok:
                all_ok = False
        except Exception as exc:
            checks["llm"] = {
                "ok": False,
                "backend": "ollama",
                "status": f"unreachable: {exc}",
                "fix": "Ensure the ollama container is running: docker compose up ollama -d",
            }
            all_ok = False
    elif backend in ("openai", "anthropic"):
        key_present = bool(
            settings.OPENAI_API_KEY if backend == "openai" else settings.ANTHROPIC_API_KEY
        )
        checks["llm"] = {
            "ok": key_present,
            "backend": backend,
            "status": "api_key_configured" if key_present else "missing_api_key",
            "fix": (
                None if key_present
                else f"Set {backend.upper()}_API_KEY in your .env file"
            ),
        }
        if not key_present:
            all_ok = False
    elif backend == "bedrock":
        checks["llm"] = {
            "ok": True,
            "backend": "bedrock",
            "status": "uses_iam_credentials",
            "model": settings.BEDROCK_MODEL_ID,
            "region": settings.BEDROCK_REGION,
        }
    elif backend == "gemini":
        key_present = bool(settings.GEMINI_API_KEY)
        checks["llm"] = {
            "ok": key_present,
            "backend": "gemini",
            "status": "api_key_configured" if key_present else "missing_api_key",
            "model": settings.GEMINI_MODEL,
            "fix": (
                None if key_present
                else "Set GEMINI_API_KEY in your .env file"
            ),
        }
        if not key_present:
            all_ok = False
    else:
        checks["llm"] = {"ok": False, "backend": backend, "status": "unknown_backend"}
        all_ok = False

    # ── 4. GitHub token (optional) ───────────────────────────────
    gh_configured = bool(settings.GITHUB_TOKEN)
    checks["github"] = {
        "ok": gh_configured,
        "status": "configured" if gh_configured else "not_configured",
        "note": (
            "Token set — PR Review and Auto-Heal PR creation are available."
            if settings.GITHUB_TOKEN
            else "GITHUB_TOKEN not set. Set it in .env to enable PR Review and Auto-Heal PR creation."
        ),
    }

    status_code = 200 if all_ok else 503
    return _JSONResponse(
        status_code=status_code,
        content={
            "ok": all_ok,
            "version": __version__,
            "llm_backend": settings.LLM_BACKEND,
            "checks": checks,
        },
    )


@app.get("/", tags=["ops"])
async def root():
    """Landing route with a quick summary of the platform."""
    return {
        "app": settings.APP_NAME,
        "version": __version__,
        "description": "AI-Native Internal Developer Platform",
        "docs": "/docs",
        "health": "/health",
    }
