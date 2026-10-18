"""
RootOps — Tiered Log Concept Service

Transforms raw log streams into LogConcept records instead of embedding every
individual log line. Achieves 10M lines → ~1K concepts → 1K embeddings.

Three-tier pipeline:
  Tier 1 — Structured JSON logs: extract fields directly, skip pattern mining.
  Tier 2 — Drain3 pattern mining: match to existing clusters or create new ones.
  Tier 3 — LLM-assisted clustering: for logs Drain3 cannot place (async, cheap).

The result is a set of LogConcept rows, each with:
  - A canonical template: "Timeout connecting to Redis after <*>ms"
  - Occurrence counts and temporal histograms
  - ONE embedding vector (updated when the concept changes significantly)
  - Trend detection (rising / stable / falling)
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.log_concept import LogConcept
from app.services.embedding import embed_batch

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Variable placeholder ──────────────────────────────────────────
# Drain3 replaces variable tokens with <*>. We use the same convention.
_VAR_PLACEHOLDER = "<*>"

# Tokens that are always treated as variables (numbers, UUIDs, IPs, hashes)
_VARIABLE_PATTERNS = [
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),  # UUID
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b"),  # IP[:port]
    re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE),  # hex hash
    re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|kb|mb|gb)?\b"),  # numbers with units
    re.compile(r"https?://\S+"),  # URLs
]


# ── Template extraction ───────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """Split log message into tokens, stripping ANSI codes."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)  # strip ANSI
    return text.split()


def _is_variable(token: str) -> bool:
    """Return True if a token looks like a variable value rather than a keyword."""
    for pattern in _VARIABLE_PATTERNS:
        if pattern.fullmatch(token):
            return True
    return False


def _extract_template(message: str) -> str:
    """
    Produce a canonical template from a log message by replacing variable
    tokens with <*>. This is a lightweight approximation of Drain3's
    log parsing step — the full Drain3 algorithm handles cluster matching.
    """
    tokens = _tokenize(message)
    result = []
    for tok in tokens:
        if _is_variable(tok):
            # Collapse consecutive placeholders into one
            if result and result[-1] == _VAR_PLACEHOLDER:
                continue
            result.append(_VAR_PLACEHOLDER)
        else:
            result.append(tok)
    return " ".join(result)


def _concept_id(service_name: str, template: str) -> str:
    """Stable ID for a (service, template) pair."""
    key = f"{service_name}:{template}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


# ── Temporal helpers ──────────────────────────────────────────────

def _current_hour_key() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H")


def _current_day_key() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


def _detect_trend(histogram: dict) -> str:
    """
    Compare the last 3 hours vs the previous 3 hours.
    Returns 'rising', 'falling', or 'stable'.
    """
    if not histogram or len(histogram) < 2:
        return "unknown"

    sorted_keys = sorted(histogram.keys())
    recent = sum(histogram[k] for k in sorted_keys[-3:])
    older = sum(histogram[k] for k in sorted_keys[-6:-3]) or 1

    ratio = recent / older
    if ratio > 1.5:
        return "rising"
    if ratio < 0.67:
        return "falling"
    return "stable"


def _trim_histogram(histogram: dict, max_hours: int) -> dict:
    """Keep only the most recent max_hours entries."""
    sorted_keys = sorted(histogram.keys())
    keep = sorted_keys[-max_hours:]
    return {k: histogram[k] for k in keep}


# ── Drain3 integration ────────────────────────────────────────────

# Per-service Drain3 LogCluster caches. Drain3 is loaded lazily.
_drain_instances: dict[str, object] = {}


def _get_drain(service_name: str) -> object:
    """Return the Drain3 LogParser for a given service, creating if needed."""
    if service_name not in _drain_instances:
        try:
            from drain3 import TemplateMiner
            from drain3.template_miner_config import TemplateMinerConfig

            config = TemplateMinerConfig()
            config.drain_sim_th = settings.LOG_CONCEPT_DRAIN_SIM_THRESHOLD
            config.drain_depth = 4
            config.drain_max_children = settings.LOG_CONCEPT_MAX_CLUSTERS
            config.parametrize_numeric_tokens = True

            miner = TemplateMiner(config=config)
            _drain_instances[service_name] = miner
        except ImportError:
            logger.warning(
                "drain3 not installed — Tier 2 mining falls back to regex templates"
            )
            _drain_instances[service_name] = None

    return _drain_instances[service_name]


def _drain_match(service_name: str, message: str) -> tuple[str | None, str | None]:
    """
    Run a message through Drain3.
    Returns (template, cluster_id) or (None, None) if drain is unavailable.
    """
    miner = _get_drain(service_name)
    if miner is None:
        return None, None

    try:
        result = miner.add_log_message(message)
        if result and result.get("cluster"):
            cluster = result["cluster"]
            template = cluster.get_template()
            cluster_id = str(cluster.cluster_id)
            return template, cluster_id
    except Exception as exc:
        logger.debug("Drain3 error for service %s: %s", service_name, exc)

    return None, None


# ── Tier 1: Structured log processing ────────────────────────────

def _is_structured(log_entry: dict) -> bool:
    """Return True if the log was parsed from JSON (has metadata_json)."""
    return log_entry.get("metadata_json") is not None or log_entry.get("source") == "otel"


def _template_from_structured(log_entry: dict) -> str:
    """Extract a template from a structured log entry without Drain3."""
    # For structured logs, strip the message of variable values
    return _extract_template(log_entry.get("message", ""))


# ── Tier 3: LLM fallback (async, batched) ─────────────────────────

async def _llm_cluster_batch(messages: list[str]) -> list[str]:
    """
    Ask the LLM to extract templates from a batch of unmatched log messages.
    Returns a list of templates (same length as messages).
    Runs asynchronously and cheaply — use the fastest available model.
    """
    from app.services.llm_backend import generate

    if not messages:
        return []

    sample = "\n".join(f"  {i+1}. {m[:200]}" for i, m in enumerate(messages[:20]))
    prompt = (
        "Extract a canonical log template for each message below. "
        "Replace variable values (numbers, UUIDs, IPs, paths) with <*>. "
        "Return one template per line, numbered to match.\n\n"
        f"Messages:\n{sample}\n\n"
        "Templates (one per line, keep the same numbering):"
    )

    try:
        response = await generate(prompt, [])
        lines = [line.strip() for line in response.strip().splitlines() if line.strip()]
        # Parse numbered lines: "1. Template text"
        templates = []
        for line in lines:
            match = re.match(r"^\d+\.\s*(.+)$", line)
            templates.append(match.group(1) if match else line)

        # Pad or trim to match input length
        while len(templates) < len(messages):
            templates.append(_extract_template(messages[len(templates)]))
        return templates[: len(messages)]

    except Exception as exc:
        logger.warning("LLM Tier-3 clustering failed: %s — falling back to regex", exc)
        return [_extract_template(m) for m in messages]


# ── Core concept update logic ─────────────────────────────────────

async def _upsert_concept(
    session: AsyncSession,
    service_name: str,
    template: str,
    cluster_id: str | None,
    severity: str | None,
    max_hours: int,
) -> LogConcept:
    """
    Find or create a LogConcept for this (service, template) and update its
    occurrence counts, temporal histograms, and trend.
    """
    concept_id = _concept_id(service_name, template)
    hour_key = _current_hour_key()
    day_key = _current_day_key()

    result = await session.execute(
        select(LogConcept).where(LogConcept.id == concept_id)
    )
    concept = result.scalar_one_or_none()

    if concept is None:
        concept = LogConcept(
            id=concept_id,
            service_name=service_name,
            template=template,
            drain_cluster_id=cluster_id,
            severity=severity,
            total_occurrences=1,
            temporal_histogram={hour_key: 1},
            daily_counts={day_key: 1},
            trend="unknown",
            embedding_model_version=settings.EMBEDDING_MODEL_VERSION,
        )
        session.add(concept)
    else:
        concept.total_occurrences += 1

        # Update hourly histogram
        hist = dict(concept.temporal_histogram or {})
        hist[hour_key] = hist.get(hour_key, 0) + 1
        hist = _trim_histogram(hist, max_hours)
        concept.temporal_histogram = hist

        # Update daily counts
        daily = dict(concept.daily_counts or {})
        daily[day_key] = daily.get(day_key, 0) + 1
        # Keep last 7 days
        sorted_days = sorted(daily.keys())
        if len(sorted_days) > 7:
            for old in sorted_days[:-7]:
                del daily[old]
        concept.daily_counts = daily

        # Recalculate trend
        concept.trend = _detect_trend(hist)
        concept.last_seen_at = datetime.now(tz=UTC)

        # Update severity if new one is more severe
        severity_rank = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "FATAL": 4, "CRITICAL": 4}
        if severity and severity_rank.get(severity, -1) > severity_rank.get(concept.severity or "", -1):
            concept.severity = severity

    return concept


# ── Public API ────────────────────────────────────────────────────

async def process_log_batch(
    session: AsyncSession,
    log_entries: list[dict],
    service_name: str,
) -> dict:
    """
    Route a batch of parsed log entries through the three-tier pipeline
    and upsert LogConcept records.

    Returns stats: {"concepts_created": N, "concepts_updated": M, "tier_stats": {...}}
    """
    max_hours = settings.LOG_CONCEPT_HISTOGRAM_HOURS
    tier_stats: dict[str, int] = defaultdict(int)
    tier3_pending: list[tuple[int, dict]] = []  # (index, entry) needing LLM

    concepts: list[LogConcept] = []

    for idx, entry in enumerate(log_entries):
        message = entry.get("message", "")
        severity = entry.get("level")
        template: str | None = None
        cluster_id: str | None = None

        # Tier 1: structured logs → direct template extraction
        if _is_structured(entry):
            template = _template_from_structured(entry)
            tier_stats["tier1"] += 1

        # Tier 2: Drain3 pattern matching
        if template is None:
            template, cluster_id = _drain_match(service_name, message)
            if template:
                tier_stats["tier2"] += 1

        # Tier 3: queue for LLM batch if neither tier matched
        if template is None:
            tier3_pending.append((idx, entry))

        if template:
            concept = await _upsert_concept(
                session, service_name, template, cluster_id, severity, max_hours
            )
            concepts.append(concept)

    # Tier 3: process unmatched batch through LLM
    if tier3_pending:
        messages = [e.get("message", "") for _, e in tier3_pending]
        templates = await _llm_cluster_batch(messages)
        tier_stats["tier3"] += len(tier3_pending)

        for (idx, entry), tmpl in zip(tier3_pending, templates):
            # Seed Drain3 with the LLM-extracted template for next time
            miner = _get_drain(service_name)
            if miner:
                with contextlib.suppress(Exception):
                    miner.add_log_message(tmpl)

            concept = await _upsert_concept(
                session, service_name, tmpl, None,
                entry.get("level"), max_hours,
            )
            concepts.append(concept)

    # ── Embed concepts that have no embedding yet ─────────────────
    needs_embedding = [c for c in concepts if c.embedding is None]
    if needs_embedding:
        texts = [c.template for c in needs_embedding]
        embeddings = await embed_batch(texts, domain="log")
        for concept, emb in zip(needs_embedding, embeddings):
            concept.embedding = emb
            concept.embedding_model_version = settings.EMBEDDING_MODEL_VERSION

    await session.flush()

    created = sum(1 for c in concepts if c.total_occurrences == 1)
    updated = len(concepts) - created

    logger.info(
        "LogConcept pipeline: %d entries → %d concepts (%d created, %d updated) | tiers: %s",
        len(log_entries), len(concepts), created, updated, dict(tier_stats),
    )

    return {
        "concepts_created": created,
        "concepts_updated": updated,
        "tier_stats": dict(tier_stats),
        "total_entries": len(log_entries),
    }


async def get_concepts_for_service(
    session: AsyncSession,
    service_name: str,
    severity: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return LogConcepts for a service, optionally filtered by severity."""
    query = select(LogConcept).where(LogConcept.service_name == service_name)
    if severity:
        query = query.where(LogConcept.severity == severity.upper())
    query = query.order_by(LogConcept.total_occurrences.desc()).limit(limit)
    result = await session.execute(query)
    return [r.to_dict() for r in result.scalars().all()]


async def get_rising_concepts(
    session: AsyncSession,
    limit: int = 20,
) -> list[dict]:
    """Return LogConcepts currently trending upward across all services."""
    query = (
        select(LogConcept)
        .where(LogConcept.trend == "rising")
        .order_by(LogConcept.total_occurrences.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    return [r.to_dict() for r in result.scalars().all()]
