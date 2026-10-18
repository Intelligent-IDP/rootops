"""
RootOps V2 — Pattern Analyzer Service

Analyses committed code by author to build per-developer style
fingerprints. Powers the "Residency" feature — Developer Pattern Cloning.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.code_chunk import CodeChunk
from app.models.commit import Commit
from app.models.dev_profile import DevProfile
from app.services.embedding import embed_text
from app.services.llm_backend import generate

logger = logging.getLogger(__name__)


async def build_developer_profiles(session: AsyncSession) -> list[dict]:
    """Scan all commits and code chunks to build/update developer profiles.

    Groups code by author, analyses patterns, and generates
    LLM-powered style summaries for each developer.

    Returns:
        List of profile summary dicts.
    """
    # ── 1. Get all unique authors ────────────────────────────────
    authors_result = await session.execute(
        select(
            Commit.author_name,
            Commit.author_email,
            func.count(Commit.id).label("commit_count"),
        )
        .group_by(Commit.author_name, Commit.author_email)
    )
    authors = authors_result.fetchall()

    if not authors:
        logger.info("No commits found — cannot build profiles")
        return []

    profiles_built: list[dict] = []

    for author in authors:
        author_name = author.author_name
        author_email = author.author_email
        commit_count = author.commit_count

        logger.info(
            "Building profile for %s (%s) — %d commits",
            author_name, author_email, commit_count,
        )

        # ── 2. Get this author's code chunks ─────────────────────
        # Chunks are all linked to HEAD sha, so join-by-commit_sha
        # only works for the HEAD author.  Instead, correlate via
        # files_changed recorded on each commit — this gives every
        # author chunks from files they actually touched.
        author_commits_result = await session.execute(
            select(Commit.files_changed)
            .where(Commit.author_email == author_email)
        )
        touched_files: set[str] = set()
        for (files_changed,) in author_commits_result:
            if files_changed:
                touched_files.update(files_changed)

        if not touched_files:
            # Fallback: try the old commit_sha join
            chunks_result = await session.execute(
                select(CodeChunk)
                .join(Commit, CodeChunk.commit_sha == Commit.sha)
                .where(Commit.author_email == author_email)
                .limit(50)
            )
        else:
            chunks_result = await session.execute(
                select(CodeChunk)
                .where(CodeChunk.file_path.in_(list(touched_files)))
                .limit(50)
            )
        chunks = chunks_result.scalars().all()

        if not chunks:
            continue

        # ── 3. Analyse patterns ──────────────────────────────────
        language_counter: Counter = Counter()
        files_touched: Counter = Counter()

        code_samples: list[str] = []
        for chunk in chunks:
            if chunk.language:
                language_counter[chunk.language] += 1
            files_touched[chunk.file_path] += 1
            code_samples.append(
                f"# {chunk.file_path}:{chunk.start_line}-{chunk.end_line}\n"
                f"{chunk.chunk_content[:500]}"
            )

        # ── 4. Generate LLM style summary ────────────────────────
        sample_text = "\n\n---\n\n".join(code_samples[:10])
        style_prompt = (
            f"Analyse the following code samples written by {author_name} "
            f"and describe their coding style in 3-5 bullet points. "
            f"Focus on: naming conventions, error handling patterns, "
            f"code structure preferences, and any notable idioms.\n\n"
            f"{sample_text}"
        )

        try:
            pattern_summary = await generate(
                style_prompt,
                [],  # No context chunks needed — the prompt IS the context
            )
        except Exception as e:
            logger.warning("LLM style analysis failed for %s: %s", author_email, e)
            pattern_summary = f"Profile for {author_name} ({commit_count} commits)"

        # ── 5. Build structured patterns ─────────────────────────
        code_patterns = {
            "languages": dict(language_counter.most_common(10)),
            "top_files": dict(files_touched.most_common(20)),
            "sample_count": len(code_samples),
        }

        # ── 6. Embed the style summary as a fingerprint ──────────
        try:
            style_embedding = await embed_text(pattern_summary)
        except Exception:
            style_embedding = None

        # ── 7. Upsert the profile ────────────────────────────────
        existing = await session.execute(
            select(DevProfile).where(DevProfile.author_email == author_email)
        )
        profile = existing.scalar_one_or_none()

        if profile:
            profile.author_name = author_name
            profile.pattern_summary = pattern_summary
            profile.code_patterns = code_patterns
            profile.commit_count = commit_count
            profile.files_touched = dict(files_touched)
            profile.primary_languages = dict(language_counter)
            profile.embedding = style_embedding
            profile.last_updated = datetime.now(UTC)
        else:
            profile = DevProfile(
                id=uuid.uuid4(),
                author_name=author_name,
                author_email=author_email,
                pattern_summary=pattern_summary,
                code_patterns=code_patterns,
                commit_count=commit_count,
                files_touched=dict(files_touched),
                primary_languages=dict(language_counter),
                embedding=style_embedding,
            )
            session.add(profile)

        await session.flush()

        profiles_built.append({
            "author_name": author_name,
            "author_email": author_email,
            "commit_count": commit_count,
            "languages": dict(language_counter.most_common(5)),
            "summary_preview": pattern_summary[:200],
        })

    await session.commit()
    logger.info("Built/updated %d developer profiles", len(profiles_built))
    return profiles_built


async def get_profile(
    author_email: str,
    session: AsyncSession,
) -> dict | None:
    """Get a single developer's profile."""
    result = await session.execute(
        select(DevProfile).where(DevProfile.author_email == author_email)
    )
    profile = result.scalar_one_or_none()

    if not profile:
        return None

    return {
        "author_name": profile.author_name,
        "author_email": profile.author_email,
        "pattern_summary": profile.pattern_summary,
        "code_patterns": profile.code_patterns,
        "commit_count": profile.commit_count,
        "files_touched": profile.files_touched,
        "primary_languages": profile.primary_languages,
        "last_updated": str(profile.last_updated),
    }


async def list_profiles(session: AsyncSession) -> list[dict]:
    """List all developer profiles."""
    result = await session.execute(
        select(DevProfile).order_by(DevProfile.commit_count.desc())
    )
    profiles = result.scalars().all()

    return [
        {
            "author_name": p.author_name,
            "author_email": p.author_email,
            "commit_count": p.commit_count,
            "primary_languages": p.primary_languages,
            "summary_preview": (
                p.pattern_summary[:200] if p.pattern_summary else None
            ),
            "last_updated": str(p.last_updated),
        }
        for p in profiles
    ]
