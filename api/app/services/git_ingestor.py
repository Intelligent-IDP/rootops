"""
RootOps — Git Repository Ingestor

Walks a local git repository, extracts commit metadata and source code,
chunks the code, embeds each chunk, and stores everything in pgvector.

Multi-repo support: every ingest call registers a Repository row and tags
all chunks/commits with its repo_id. This enables per-repo scoping and
the cross-repo dependency graph.
"""

from __future__ import annotations

import gc
import hashlib
import logging
import shutil
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from git import Repo as GitRepo
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.code_chunk import CodeChunk
from app.models.commit import Commit
from app.models.repository import Repository
from app.services.chunker import ChunkResult, chunk_file, is_supported_file
from app.services.embedding import embed_batch

logger = logging.getLogger(__name__)
settings = get_settings()


def _with_github_token(repo_url: str) -> str:
    """Inject token into GitHub HTTPS URL when provided via env."""
    token = settings.GITHUB_TOKEN
    if not token:
        return repo_url
    if not repo_url.startswith("https://github.com/"):
        return repo_url
    if "@github.com" in repo_url:
        return repo_url
    return repo_url.replace("https://", f"https://{token}@", 1)


async def _register_repository(
    session: AsyncSession,
    *,
    name: str,
    url: str | None = None,
    local_path: str | None = None,
    team: str | None = None,
    tags: list | None = None,
    description: str | None = None,
) -> Repository:
    """Upsert a Repository row, returning the row (new or existing)."""
    # Check if a repo with this name already exists
    existing = (
        await session.execute(select(Repository).where(Repository.name == name))
    ).scalar_one_or_none()

    if existing:
        # Update mutable fields
        if url is not None:
            existing.url = url
        if local_path is not None:
            existing.local_path = local_path
        if team is not None:
            existing.team = team
        if tags is not None:
            existing.tags = tags
        if description is not None:
            existing.description = description
        await session.flush()
        return existing

    repo = Repository(
        id=uuid.uuid4(),
        name=name,
        url=url,
        local_path=local_path,
        team=team,
        tags=tags,
        description=description,
    )
    session.add(repo)
    await session.flush()
    return repo


async def clone_and_ingest(
    repo_url: str,
    session: AsyncSession,
    *,
    branch: str = "HEAD",
    max_commits: int | None = None,
    repo_name: str | None = None,
    team: str | None = None,
    tags: list | None = None,
    description: str | None = None,
) -> dict:
    """Clone a remote repository and ingest it into the Semantic Engine."""
    # Derive a name from the URL if not provided
    if not repo_name:
        repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    # Create a deterministic temp dir based on the URL
    url_hash = hashlib.md5(repo_url.encode()).hexdigest()[:12]
    clone_dir = Path(tempfile.gettempdir()) / "rootops-repos" / url_hash

    try:
        if clone_dir.exists():
            shutil.rmtree(clone_dir)

        clone_url = _with_github_token(repo_url)
        logger.info("Cloning %s → %s", repo_url, clone_dir)
        clone_dir.mkdir(parents=True, exist_ok=True)

        clone_kwargs = {"depth": max_commits or 100}
        if branch and branch != "HEAD":
            clone_kwargs["branch"] = branch

        GitRepo.clone_from(clone_url, str(clone_dir), **clone_kwargs)
        logger.info("Clone complete, starting ingestion")

        stats = await ingest_repository(
            repo_path=str(clone_dir),
            session=session,
            branch=branch,
            max_commits=max_commits,
            repo_name=repo_name,
            repo_url=repo_url,
            team=team,
            tags=tags,
            description=description,
        )
        stats["repo_url"] = repo_url
        return stats

    finally:
        if clone_dir.exists():
            logger.info("Cleaning up clone dir: %s", clone_dir)
            shutil.rmtree(clone_dir, ignore_errors=True)


async def ingest_repository(
    repo_path: str,
    session: AsyncSession,
    *,
    branch: str = "HEAD",
    max_commits: int | None = None,
    repo_name: str | None = None,
    repo_url: str | None = None,
    team: str | None = None,
    tags: list | None = None,
    description: str | None = None,
) -> dict:
    """Ingest an entire git repository into the Semantic Engine.

    Registers the repository in the repositories table, tags all chunks and
    commits with its repo_id, runs dependency extraction, and generates a
    per-repo codebase summary.

    Returns:
        Summary dict with counts of ingested commits, chunks, and dependencies.
    """
    # ── 0. Register / update the repository row ──────────────────
    if not repo_name:
        repo_name = Path(repo_path).name

    repo_record = await _register_repository(
        session,
        name=repo_name,
        url=repo_url,
        local_path=repo_path,
        team=team,
        tags=tags,
        description=description,
    )
    repo_id = repo_record.id
    logger.info("Ingesting repository '%s' (id=%s)", repo_name, repo_id)

    git_repo = GitRepo(repo_path)
    stats: dict = {
        "repo_id": str(repo_id),
        "repo_name": repo_name,
        "commits_ingested": 0,
        "chunks_ingested": 0,
        "files_processed": 0,
        "dependencies_found": 0,
    }

    # ── 1. Ingest commit history ─────────────────────────────────
    logger.info("Starting commit history ingestion from %s", repo_path)
    commits_iter = git_repo.iter_commits(branch, max_count=max_commits)

    for git_commit in commits_iter:
        existing = await session.execute(
            select(Commit).where(Commit.sha == git_commit.hexsha)
        )
        if existing.scalar_one_or_none():
            continue

        commit_record = Commit(
            id=uuid.uuid4(),
            repo_id=repo_id,
            sha=git_commit.hexsha,
            message=git_commit.message.strip(),
            author_name=git_commit.author.name or "Unknown",
            author_email=git_commit.author.email or "",
            committed_at=datetime.fromtimestamp(
                git_commit.committed_date, tz=UTC
            ),
            branch=branch if branch != "HEAD" else _get_active_branch(git_repo),
            files_changed=_get_changed_files(git_commit),
        )
        session.add(commit_record)
        stats["commits_ingested"] += 1

    await session.flush()
    logger.info("Ingested %d commits", stats["commits_ingested"])

    # ── 2. Ingest current tree (code chunks) ─────────────────────
    logger.info("Starting code chunking for current tree")
    head_sha = git_repo.head.commit.hexsha
    tree = git_repo.head.commit.tree

    all_chunks: list[tuple[ChunkResult, str]] = []
    # Only collect full file contents when dep extraction is enabled;
    # this list holds every file's text and can be large.
    collect_deps = not settings.DISABLE_DEP_EXTRACTION
    file_contents_for_deps: list[tuple[str, str]] = []

    for blob in _walk_tree(tree):
        file_path = blob.path
        if not is_supported_file(file_path):
            continue

        try:
            content = blob.data_stream.read().decode("utf-8", errors="ignore")
        except Exception:
            logger.warning("Could not read %s, skipping", file_path)
            continue

        if not content.strip():
            continue

        chunks = chunk_file(content, file_path)
        for chunk in chunks:
            chunk.file_path = file_path
            all_chunks.append((chunk, head_sha))

        if collect_deps:
            file_contents_for_deps.append((file_path, content))
        stats["files_processed"] += 1

    logger.info(
        "Chunked %d files into %d chunks",
        stats["files_processed"],
        len(all_chunks),
    )

    # ── 3. Embed & store chunks in batches ─────────────────────────
    # Process in sub-batches to limit peak memory: embed a batch,
    # write it to the DB, then release that memory before the next.
    if all_chunks:
        logger.info("Embedding %d chunks...", len(all_chunks))

        EMBED_SUB_BATCH = settings.EMBED_BATCH_SIZE  # default 64; set lower on small VMs

        for i in range(0, len(all_chunks), EMBED_SUB_BATCH):
            batch = all_chunks[i : i + EMBED_SUB_BATCH]
            texts = [c.content[: settings.EMBED_MAX_CHARS] for c, _ in batch]
            logger.info(
                "  Embedding batch %d-%d of %d",
                i + 1, min(i + EMBED_SUB_BATCH, len(all_chunks)), len(all_chunks),
            )
            embeddings = await embed_batch(texts)

            # Store this sub-batch immediately
            for (chunk, commit_sha), embedding in zip(batch, embeddings):
                existing = await session.execute(
                    select(CodeChunk).where(
                        CodeChunk.file_path == chunk.file_path,
                        CodeChunk.start_line == chunk.start_line,
                        CodeChunk.commit_sha == commit_sha,
                    )
                )
                if existing.scalar_one_or_none():
                    continue

                chunk_record = CodeChunk(
                    id=uuid.uuid4(),
                    repo_id=repo_id,
                    file_path=chunk.file_path,
                    chunk_content=chunk.content,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    language=chunk.language,
                    embedding=embedding,
                    commit_sha=commit_sha,
                )
                session.add(chunk_record)
                stats["chunks_ingested"] += 1

            # Commit this sub-batch and evict ORM objects from the
            # session identity map so their Python-side memory (especially
            # the 768-dim embedding lists) is freed before the next batch.
            await session.commit()
            session.expire_all()
            del embeddings, texts, batch
            gc.collect()
            logger.info("  Batch committed, session cleared")

    # ── 5. Update repository counters ────────────────────────────
    # Re-fetch the repo record because session.expire_all() in the
    # embedding loop invalidated it.  Accessing expired attributes on an
    # async session triggers the "greenlet_spawn" error.
    repo_record = await session.get(Repository, repo_id)
    repo_record.chunk_count = (
        repo_record.chunk_count + stats["chunks_ingested"]
    )
    repo_record.commit_count = (
        repo_record.commit_count + stats["commits_ingested"]
    )
    repo_record.last_ingested_at = datetime.now(UTC)
    await session.flush()

    await session.commit()
    logger.info(
        "Ingestion complete: %d commits, %d chunks from %d files",
        stats["commits_ingested"],
        stats["chunks_ingested"],
        stats["files_processed"],
    )

    # ── 6. Dependency extraction ─────────────────────────────────
    if settings.DISABLE_DEP_EXTRACTION:
        logger.info("Dependency extraction disabled (DISABLE_DEP_EXTRACTION=true) — skipping")
    else:
        try:
            dep_count = await _extract_dependency_graph(
                session,
                repo_id=repo_id,
                repo_name=repo_name,
                repo_path=repo_path,
                file_contents=file_contents_for_deps,
            )
            await session.commit()
            stats["dependencies_found"] = dep_count
            logger.info("Dependency extraction complete: %d dependencies", dep_count)
        except Exception:
            await session.rollback()
            logger.warning("Dependency extraction failed — ingestion still succeeded", exc_info=True)

    # Free large in-memory structures before calling the LLM (which
    # causes Ollama to load the model into RAM).
    del file_contents_for_deps
    del all_chunks
    gc.collect()

    # ── 7. Generate codebase summary ─────────────────────────────
    if settings.DISABLE_CODEBASE_SUMMARY:
        logger.info("Codebase summary disabled (DISABLE_CODEBASE_SUMMARY=true) — skipping")
    else:
        try:
            await _generate_codebase_summary(session, repo_id=repo_id, repo_path=repo_path)
        except Exception:
            logger.warning("Codebase summary generation failed — queries will still work")

    return stats


async def _generate_codebase_summary(
    session: AsyncSession,
    *,
    repo_id: uuid.UUID,
    repo_path: str,
    sample_size: int = 30,
) -> None:
    """Generate and persist an LLM architectural summary for a repository."""
    from sqlalchemy import text as sa_text

    from app.models.codebase_summary import CodebaseSummary
    from app.services.llm_backend import generate

    sample_sql = sa_text("""
        SELECT DISTINCT ON (file_path)
            file_path, chunk_content, language, start_line
        FROM code_chunks
        WHERE embedding IS NOT NULL
          AND repo_id = :repo_id
        ORDER BY file_path, start_line
        LIMIT :n
    """)
    rows = (await session.execute(sample_sql, {"repo_id": repo_id, "n": sample_size})).fetchall()

    if not rows:
        logger.info("No chunks to summarise yet — skipping codebase summary")
        return

    listing = "\n\n".join(
        f"### {row.file_path} (L{row.start_line})\n"
        f"```{row.language or ''}\n{row.chunk_content[:400]}\n```"
        for row in rows
    )

    summary_prompt = (
        "You are analysing a software codebase. Based on the code samples below, "
        "produce a concise structured architectural summary covering:\n"
        "1. What this service/application does\n"
        "2. Key components and their responsibilities (list each with file path)\n"
        "3. Data flow (how a request moves through the system)\n"
        "4. External dependencies (databases, APIs, queues)\n"
        "5. Any notable patterns, risks, or areas of fragility\n\n"
        "Be precise and factual. Reference actual file paths and function names.\n\n"
        f"## Code Samples\n\n{listing}"
    )

    logger.info("Generating codebase summary from %d sample chunks…", len(rows))
    summary_text = await generate(summary_prompt, [])

    existing = await session.get(CodebaseSummary, repo_id)
    if existing:
        existing.summary = summary_text
        existing.repo_path = repo_path
        existing.chunk_count = len(rows)
        existing.generated_at = datetime.now(UTC)
    else:
        session.add(CodebaseSummary(
            repo_id=repo_id,
            summary=summary_text,
            repo_path=repo_path,
            chunk_count=len(rows),
        ))

    await session.commit()
    logger.info("Codebase summary stored (%d chars)", len(summary_text))


def _get_active_branch(repo: GitRepo) -> str | None:
    """Get the current active branch name, or None if detached."""
    try:
        return repo.active_branch.name
    except TypeError:
        return None


def _get_changed_files(git_commit) -> list[str]:
    """Extract list of changed file paths from a git commit."""
    try:
        if git_commit.parents:
            diffs = git_commit.diff(git_commit.parents[0])
        else:
            diffs = git_commit.diff(None)
        return [d.a_path or d.b_path for d in diffs if d.a_path or d.b_path]
    except Exception:
        return []


def _walk_tree(tree):
    """Recursively yield all blobs (files) from a git tree."""
    for item in tree.traverse():
        if item.type == "blob":
            yield item


async def _extract_dependency_graph(
    session: AsyncSession,
    *,
    repo_id: uuid.UUID,
    repo_name: str,
    repo_path: str,
    file_contents: list[tuple[str, str]],
) -> int:
    """Build the dependency graph with Breadth, falling back to regex.

    DEP_GRAPH_SOURCE picks the extractor: "breadth", "regex" or "auto".
    Returns the number of dependency rows written.
    """
    source = settings.DEP_GRAPH_SOURCE.lower().strip()
    if source in ("breadth", "auto"):
        from app.services.breadth_graph import import_flowgraph, scan_repository

        flowgraph = await scan_repository(repo_path)
        if flowgraph is not None:
            result = await import_flowgraph(
                session, flowgraph, repo_id=repo_id, repo_name=repo_name,
            )
            return result["dependencies"]
        if source == "breadth":
            raise RuntimeError(
                "DEP_GRAPH_SOURCE=breadth but Breadth is not installed or the scan failed "
                f"(BREADTH_BIN={settings.BREADTH_BIN!r})"
            )
        logger.info("Breadth not available — using the regex dependency extractor")

    from app.services.dep_extractor import extract_and_persist_dependencies

    return await extract_and_persist_dependencies(
        session,
        source_repo_id=repo_id,
        source_repo_name=repo_name,
        file_contents=file_contents,
    )
