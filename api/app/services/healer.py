"""
RootOps — Auto-Healing Engine

The capstone intelligence feature. Scans ingested logs for errors,
correlates them with code, asks the LLM to diagnose the root cause
and suggest a fix, then optionally opens a GitHub PR with the patch.

Pipeline:
1. Scan log_entries for recent ERROR/WARN patterns
2. Cross-reference with code_chunks to find responsible code
3. Ask LLM to diagnose and generate a fix (+ rollback plan)
4. Score confidence and blast radius → trust ladder
5. Persist the diagnosis to PostgreSQL (can be pushed as a PR only
   when trust ladder permits)

Trust Ladder
─────────────────────────────────────────────────────────────
confidence × blast_radius → action

HIGH confidence + LOW blast_radius  → auto_apply_eligible = True
HIGH confidence + HIGH blast_radius → PR created, requires_approval = True
LOW  confidence + any               → diagnosis only, no PR action
ANY  + production                   → requires_approval = True always
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import delete, select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.log_entry import LogEntry
from app.models.pending_fix import PendingFix
from app.services.llm_backend import generate

settings = get_settings()
logger = logging.getLogger(__name__)


def _extract_code_blocks(text: str) -> str:
    """Extract fenced code blocks from markdown LLM output."""
    blocks = re.findall(r"```(?:\w*)\n(.*?)```", text, re.DOTALL)
    return "\n\n".join(blocks).strip() if blocks else ""


def _extract_section(text: str, heading: str) -> str:
    """Extract a named section from structured LLM output."""
    pattern = re.compile(
        rf"\*{{0,2}}{re.escape(heading)}\*{{0,2}}[:\s]+(.*?)(?=\n\n\*{{0,2}}[A-Z]|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(text)
    return match.group(1).strip() if match else ""


async def _compute_blast_radius(
    session: AsyncSession,
    service_name: str,
) -> tuple[int, str]:
    """
    Count the number of downstream services that depend on service_name.
    Returns (count, level) where level is "low" | "medium" | "high".
    """
    try:
        from app.models.service_dependency import ServiceDependency
        result = await session.execute(
            select(func.count()).select_from(ServiceDependency).where(
                ServiceDependency.target_repo_name == service_name.lower()
            )
        )
        count = result.scalar_one_or_none() or 0
    except Exception:
        count = 0

    high_thresh = settings.HEAL_BLAST_RADIUS_HIGH_THRESHOLD
    med_thresh = settings.HEAL_BLAST_RADIUS_MEDIUM_THRESHOLD

    if count >= high_thresh:
        level = "high"
    elif count >= med_thresh:
        level = "medium"
    else:
        level = "low"

    return count, level


def _compute_confidence(
    top_similarity: float,
    num_related: int,
    diagnosis_text: str,
) -> float:
    """
    Compute a composite confidence score (0–1) from:
    - Embedding similarity quality
    - Number of corroborating code matches
    - LLM output quality signals (length, has code block)
    """
    sim_score = min(top_similarity, 1.0)

    # More corroborating chunks → higher confidence (diminishing returns)
    context_score = min(num_related / 5.0, 1.0)

    # LLM output quality: penalise very short or vague answers
    has_code = 1.0 if "```" in diagnosis_text else 0.5
    length_score = min(len(diagnosis_text) / 500.0, 1.0)
    llm_score = (has_code + length_score) / 2.0

    # Weighted composite
    confidence = (sim_score * 0.5) + (context_score * 0.2) + (llm_score * 0.3)
    return round(min(confidence, 1.0), 3)


def _trust_ladder(
    confidence: float,
    blast_radius_level: str,
) -> tuple[bool, bool]:
    """
    Apply the trust ladder.

    Returns (requires_approval, auto_apply_eligible).
    auto_apply_eligible is only True for LOW blast_radius + HIGH confidence.
    """
    min_confidence = settings.HEAL_AUTO_APPLY_MIN_CONFIDENCE

    if blast_radius_level == "high":
        return True, False  # always needs approval

    if confidence >= min_confidence and blast_radius_level == "low":
        return False, True  # staging-eligible, no approval needed

    if confidence >= min_confidence and blast_radius_level == "medium":
        return True, False  # PR allowed but needs sign-off

    return True, False  # default: approval required


async def diagnose(session: AsyncSession, service_name: str | None = None) -> list[dict]:
    """Scan recent error logs, correlate with code, and generate diagnoses.

    Clears previous fixes and writes new ones to the DB so results are
    always fresh and visible across all API instances.

    Returns:
        List of diagnosis dicts with trust-ladder fields populated.
    """
    # Guard: LLM required for diagnosis
    if not settings.LLM_AVAILABLE:
        backend = settings.LLM_BACKEND.lower()
        logger.info("Auto-heal skipped: LLM unavailable (backend=%s, no API key)", backend)
        return [{
            "error": (
                "Auto-heal requires an LLM but no API key is configured.  "
                f"Set {backend.upper()}_API_KEY or switch LLM_BACKEND to a "
                "provider you have credentials for."
            )
        }]

    await session.execute(delete(PendingFix))
    await session.commit()

    # ── 1. Get recent ERROR/WARN log entries ──────────────────────
    log_query = (
        select(LogEntry)
        .where(LogEntry.level.in_(["ERROR", "CRITICAL", "FATAL", "WARN"]))
    )
    if service_name:
        log_query = log_query.where(LogEntry.service_name == service_name)
    log_query = log_query.order_by(LogEntry.created_at.desc()).limit(10)

    error_logs = (await session.execute(log_query)).scalars().all()

    if not error_logs:
        logger.info("No error logs found — nothing to heal")
        return []

    diagnoses: list[dict] = []

    for log_entry in error_logs:
        # ── 2. Find related code via embedding similarity ──────────
        if log_entry.embedding is None:
            continue

        embedding_str = "[" + ",".join(str(v) for v in log_entry.embedding.to_list()) + "]"

        code_sql = text("""
            SELECT
                file_path,
                chunk_content,
                start_line,
                end_line,
                language,
                1 - (embedding <=> CAST(:embedding AS halfvec)) AS similarity
            FROM code_chunks
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS halfvec)
            LIMIT 5
        """)
        related_code = (
            await session.execute(code_sql, {"embedding": embedding_str})
        ).fetchall()

        if not related_code:
            continue

        top_similarity = related_code[0].similarity if related_code else 0.0
        if top_similarity < settings.HEAL_MIN_SIMILARITY:
            logger.info(
                "Skipping log %s — top similarity %.3f < %.2f threshold",
                log_entry.id, top_similarity, settings.HEAL_MIN_SIMILARITY,
            )
            continue

        # ── 3. Build diagnosis context ─────────────────────────────
        error_context = (
            f"**Error Log:**\n"
            f"- Service: {log_entry.service_name}\n"
            f"- Level: {log_entry.level}\n"
            f"- Message: {log_entry.message}\n"
            f"- Parsed Error: {log_entry.parsed_error or 'N/A'}\n"
            f"- File Reference: {log_entry.file_reference or 'N/A'}\n"
            f"- Line Reference: {log_entry.line_reference or 'N/A'}\n"
        )

        context_chunks = [
            {
                "file_path": row.file_path,
                "content": row.chunk_content,
                "start_line": row.start_line,
                "end_line": row.end_line,
                "language": row.language or "",
                "similarity": float(row.similarity),
            }
            for row in related_code
        ]

        # ── 4. Ask LLM for diagnosis, fix, AND rollback plan ───────
        heal_prompt = (
            f"You are RootOps Auto-Healer. A production error has been "
            f"correlated with the code chunks provided as context.\n\n"
            f"{error_context}\n\n"
            f"Using the code context above, provide ALL of the following:\n"
            f"1. **Root Cause**: What is causing this error?\n"
            f"2. **Suggested Fix**: Show the corrected code in a fenced code block.\n"
            f"3. **Risk Assessment**: Is this fix safe to auto-apply? (low/medium/high risk)\n"
            f"4. **File to Fix**: Which file and what lines need changing?\n"
            f"5. **Rollback Plan**: How to revert this change if it causes issues?\n"
        )

        try:
            diagnosis_text = await generate(heal_prompt, context_chunks)
        except Exception as e:
            logger.warning("LLM diagnosis failed for log %s: %s", log_entry.id, e)
            continue

        # ── 5. Score trust ladder ──────────────────────────────────
        confidence = _compute_confidence(
            top_similarity, len(related_code), diagnosis_text
        )
        blast_radius_count, blast_radius_level = await _compute_blast_radius(
            session, log_entry.service_name or ""
        )
        requires_approval, auto_apply_eligible = _trust_ladder(
            confidence, blast_radius_level
        )
        rollback_plan = _extract_section(diagnosis_text, "Rollback Plan")

        # ── 6. Persist ─────────────────────────────────────────────
        fix_id = str(uuid.uuid4())[:8]
        top_match = related_code[0]
        suggested_code = _extract_code_blocks(diagnosis_text)

        fix = PendingFix(
            fix_id=fix_id,
            error_level=log_entry.level,
            error_message=log_entry.message[:200],
            error_service=log_entry.service_name,
            file_reference=log_entry.file_reference,
            related_file=top_match.file_path,
            related_lines=f"{top_match.start_line}-{top_match.end_line}",
            similarity_score=round(top_similarity, 4),
            diagnosis=diagnosis_text,
            suggested_code=suggested_code,
            original_code=top_match.chunk_content,
            confidence_score=confidence,
            blast_radius=blast_radius_count,
            blast_radius_level=blast_radius_level,
            requires_approval=requires_approval,
            auto_apply_eligible=auto_apply_eligible,
            rollback_plan=rollback_plan or None,
        )
        session.add(fix)
        await session.commit()
        diagnoses.append(fix.to_dict())

    logger.info(
        "Generated %d diagnoses (auto-apply eligible: %d)",
        len(diagnoses),
        sum(1 for d in diagnoses if d.get("auto_apply_eligible")),
    )
    return diagnoses


async def get_pending_fixes(session: AsyncSession) -> list[dict]:
    """Return all pending fix suggestions from the DB."""
    result = await session.execute(
        select(PendingFix).order_by(PendingFix.created_at.desc())
    )
    return [row.to_dict() for row in result.scalars().all()]


async def get_fix(session: AsyncSession, fix_id: str) -> dict | None:
    """Get a specific pending fix by ID from the DB."""
    fix = await session.get(PendingFix, fix_id)
    return fix.to_dict() if fix else None
