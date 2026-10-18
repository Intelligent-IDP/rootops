"""
RootOps — RAG Engine (Retrieval-Augmented Generation)

The core intelligence layer. Pipeline:

1. Query Planner — classify query type and build a typed retrieval plan
2. HyDE — ask the LLM to imagine what the answer looks like,
           embed THAT instead of the raw question (code-to-code search)
3. Embed the (hypothetical) query — domain-specific model per layer
4. Parallel retrieval across three layers:
   Layer 1 (Vector): code chunks (code model) + log entries (log model)
   Layer 2 (Concepts): LogConcepts for pattern-level temporal understanding
   Layer 3 (Graph): knowledge graph traversal for relational reasoning
5. Cross-correlate: boost code chunks referenced by matching log errors
6. Rerank — cross-encoder scores (query, chunk) pairs for precision
7. Inject codebase summary into system prompt
8. Pass conversation history to the LLM (genuine multi-turn reasoning)
9. Synthesise via LLM
10. Return answer + sources + log matches + query plan metadata

Multi-repo: pass `repo_ids` to scope search to specific repositories.
Omit to search globally across the entire platform.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.embedding import embed_text, rerank
from app.services.llm_backend import generate, generate_stream
from app.services.query_planner import (
    RetrievalPlan,
    plan_retrieval,
    plan_to_metadata,
)

logger = logging.getLogger(__name__)
settings = get_settings()


# ── HyDE ─────────────────────────────────────────────────────────

async def _hyde_query(question: str) -> str:
    """Generate a hypothetical code answer to improve retrieval (HyDE).

    Embedding a plausible code answer and searching code-to-code is
    far more accurate than searching question-to-code, because the
    embedding model was trained on code, not on Q&A pairs.

    Falls back to the original question if the LLM is unavailable.
    """
    hyde_prompt = (
        "Write a short, realistic code snippet or technical explanation "
        "that directly answers this question about a software codebase. "
        "Include variable names, function signatures, or error messages "
        "that would plausibly appear in real source code.\n\n"
        f"Question: {question}\n\n"
        "Answer (code or explanation only, no preamble):"
    )
    try:
        hypothetical = await generate(hyde_prompt, [])
        logger.debug("HyDE generated %d chars for query", len(hypothetical))
        return hypothetical
    except Exception as exc:
        logger.warning("HyDE failed (%s) — using original question", exc)
        return question


# ── Codebase summary loader ───────────────────────────────────────

async def _load_codebase_summary(
    session: AsyncSession,
    repo_ids: list[uuid.UUID] | None = None,
) -> str | None:
    """Load the LLM-generated architectural summary from the DB.

    If repo_ids are specified, concatenates summaries for those repos.
    Otherwise returns the first valid summary available (or all combined
    if there are multiple).
    """
    from sqlalchemy import select

    from app.models.codebase_summary import CodebaseSummary

    if repo_ids:
        rows = (
            await session.execute(
                select(CodebaseSummary).where(CodebaseSummary.repo_id.in_(repo_ids))
            )
        ).scalars().all()
    else:
        rows = (await session.execute(select(CodebaseSummary))).scalars().all()

    valid = [r for r in rows if r.is_valid()]
    if not valid:
        return None

    if len(valid) == 1:
        return valid[0].summary

    # Multiple repos: stitch summaries together with repo path headings
    parts = [
        f"## Repository: {r.repo_path or str(r.repo_id)}\n\n{r.summary}"
        for r in valid
    ]
    return "\n\n---\n\n".join(parts)


# ── SQL helpers ───────────────────────────────────────────────────

def _repo_filter_clause(repo_ids: list[uuid.UUID] | None, table: str = "") -> str:
    """Return an AND clause for repo_id filtering, or empty string."""
    if not repo_ids:
        return ""
    prefix = f"{table}." if table else ""
    ids = ", ".join(f"'{r}'" for r in repo_ids)
    return f"AND {prefix}repo_id IN ({ids})"


# ── LogConcept retrieval ──────────────────────────────────────────

async def _search_log_concepts(
    session: AsyncSession,
    embedding_str: str,
    fetch_k: int,
    similarity_threshold: float,
) -> list[dict]:
    """Retrieve LogConcepts via vector similarity (log embedding space)."""
    try:
        sql = text("""
            SELECT
                id,
                service_name,
                template,
                severity,
                total_occurrences,
                trend,
                temporal_histogram,
                daily_counts,
                correlations,
                1 - (embedding <=> CAST(:embedding AS halfvec)) AS similarity
            FROM log_concepts
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:embedding AS halfvec)
            LIMIT :fetch_k
        """)
        rows = (await session.execute(sql, {"embedding": embedding_str, "fetch_k": fetch_k})).fetchall()
        return [
            {
                "id": row.id,
                "service_name": row.service_name,
                "template": row.template,
                "severity": row.severity,
                "total_occurrences": row.total_occurrences,
                "trend": row.trend,
                "temporal_histogram": row.temporal_histogram,
                "daily_counts": row.daily_counts,
                "correlations": row.correlations,
                "similarity": float(row.similarity),
                "type": "log_concept",
            }
            for row in rows
            if float(row.similarity) >= similarity_threshold
        ]
    except Exception as exc:
        logger.warning("LogConcept retrieval failed: %s", exc)
        return []


# ── Graph context retrieval ───────────────────────────────────────

async def _search_graph_context(
    session: AsyncSession,
    entities: list[str],
    direction: str,
    depth: int,
) -> list[dict]:
    """
    Retrieve knowledge graph edges for the named entities.
    Returns edge dicts that the LLM can reason over.
    """
    if not entities:
        return []
    try:
        from app.services.causation_service import get_causal_chain
        results: list[dict] = []
        for entity in entities[:3]:  # cap to avoid over-fetching
            chain = await get_causal_chain(
                session, entity,
                direction=direction,
                min_promotion_level="correlates_with",
                max_depth=depth,
            )
            results.extend(chain)
        return results
    except Exception as exc:
        logger.warning("Graph context retrieval failed: %s", exc)
        return []


# ── Main query function ───────────────────────────────────────────

async def query_codebase(
    question: str,
    session: AsyncSession,
    *,
    top_k: int = 5,
    similarity_threshold: float | None = None,
    use_llm: bool = True,
    conversation_history: list[dict] | None = None,
    repo_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Ask a question about the codebase using Hybrid RAG.

    Full pipeline: QueryPlanner → HyDE → embed (domain-split) →
    parallel retrieval (code + logs + concepts + graph) → cross-correlate
    → rerank → codebase summary → LLM synthesis.

    Args:
        question: Natural language query.
        session: Async DB session.
        top_k: Final number of sources to pass to the LLM.
        similarity_threshold: Minimum cosine similarity (defaults to config).
        use_llm: If False, return raw vector search results only.
        conversation_history: Prior turns as [{role, content}] dicts.
        repo_ids: Scope search to specific repos. None = global (all repos).
    """
    if similarity_threshold is None:
        similarity_threshold = settings.RAG_SIMILARITY_THRESHOLD

    # ── Step 1: Query Planner ─────────────────────────────────────────────
    plan: RetrievalPlan | None = None
    if settings.QUERY_PLANNER_ENABLED:
        plan = plan_retrieval(question)
        if plan.similarity_threshold_override is not None:
            similarity_threshold = plan.similarity_threshold_override
        logger.info(
            "Query plan: type=%s entities=%s layers=%s",
            plan.query_type.value,
            plan.target_entities,
            {k: v for k, v in plan_to_metadata(plan)["layers"].items() if v},
        )

    fetch_k = settings.RERANKER_CANDIDATES if settings.RERANKER_ENABLED else top_k
    code_fetch_k = plan.code_fetch_k if plan else fetch_k
    log_fetch_k = plan.log_fetch_k if plan else fetch_k
    concept_fetch_k = plan.concept_fetch_k if plan else 10

    # ── Step 2: HyDE ─────────────────────────────────────────────────────
    if use_llm and settings.HYDE_ENABLED and settings.LLM_AVAILABLE:
        search_text = await _hyde_query(question)
    else:
        search_text = question

    # ── Step 3: Embed — domain-split ─────────────────────────────────────
    logger.info("Embedding query (HyDE=%s): %s…", settings.HYDE_ENABLED, question[:80])
    # Code model for code retrieval
    code_embedding = await embed_text(search_text, domain="code")
    code_emb_str = "[" + ",".join(str(v) for v in code_embedding) + "]"
    # Log model for log/concept retrieval (may be the same model if unconfigured)
    log_embedding = await embed_text(search_text, domain="log")
    log_emb_str = "[" + ",".join(str(v) for v in log_embedding) + "]"

    repo_clause = _repo_filter_clause(repo_ids)

    # ── Step 4a: Code chunks (Layer 1) ────────────────────────────────────
    sources: list[dict] = []
    if not plan or plan.use_code_retrieval:
        code_sql = text(f"""
            SELECT
                id,
                repo_id,
                file_path,
                chunk_content,
                start_line,
                end_line,
                language,
                commit_sha,
                1 - (embedding <=> CAST(:embedding AS halfvec)) AS similarity
            FROM code_chunks
            WHERE embedding IS NOT NULL
            {repo_clause}
            ORDER BY embedding <=> CAST(:embedding AS halfvec)
            LIMIT :fetch_k
        """)
        code_rows = (await session.execute(
            code_sql, {"embedding": code_emb_str, "fetch_k": code_fetch_k}
        )).fetchall()
        sources = [
            {
                "repo_id": str(row.repo_id) if row.repo_id else None,
                "file_path": row.file_path,
                "content": row.chunk_content,
                "start_line": row.start_line,
                "end_line": row.end_line,
                "language": row.language,
                "commit_sha": row.commit_sha,
                "similarity": float(row.similarity),
            }
            for row in code_rows
            if float(row.similarity) >= similarity_threshold
        ]
        logger.info("Vector search: %d code chunks above threshold %.2f", len(sources), similarity_threshold)

    # ── Step 4b: Log entries (Layer 1) ───────────────────────────────────
    log_matches: list[dict] = []
    if not plan or plan.use_log_retrieval:
        log_repo_clause = _repo_filter_clause(repo_ids)
        log_sql = text(f"""
            SELECT
                id,
                service_name,
                timestamp,
                level,
                message,
                parsed_error,
                file_reference,
                line_reference,
                1 - (embedding <=> CAST(:embedding AS halfvec)) AS similarity
            FROM log_entries
            WHERE embedding IS NOT NULL
            {log_repo_clause}
            ORDER BY embedding <=> CAST(:embedding AS halfvec)
            LIMIT :fetch_k
        """)
        log_rows = (await session.execute(
            log_sql, {"embedding": log_emb_str, "fetch_k": log_fetch_k}
        )).fetchall()
        log_matches = [
            {
                "service_name": row.service_name,
                "timestamp": str(row.timestamp) if row.timestamp else None,
                "level": row.level,
                "message": row.message,
                "parsed_error": row.parsed_error,
                "file_reference": row.file_reference,
                "line_reference": row.line_reference,
                "similarity": float(row.similarity),
            }
            for row in log_rows
            if float(row.similarity) >= similarity_threshold
        ]
        logger.info("Vector search: %d log entries above threshold", len(log_matches))

    # ── Step 4c: LogConcepts (Layer 2) ────────────────────────────────────
    concept_matches: list[dict] = []
    if plan and plan.use_concept_retrieval:
        concept_matches = await _search_log_concepts(
            session, log_emb_str, concept_fetch_k, similarity_threshold
        )
        logger.info("Concept search: %d LogConcepts above threshold", len(concept_matches))

    # ── Step 4d: Knowledge Graph (Layer 3) ───────────────────────────────
    graph_context: list[dict] = []
    if plan and plan.use_graph_traversal and plan.target_entities:
        graph_context = await _search_graph_context(
            session,
            plan.target_entities,
            plan.graph_direction,
            plan.graph_depth,
        )
        logger.info("Graph traversal: %d edges retrieved", len(graph_context))

    # ── Step 5: Cross-correlate logs → code ──────────────────────────────
    for log_match in log_matches:
        if log_match.get("file_reference"):
            for source in sources:
                if log_match["file_reference"] in source["file_path"]:
                    source["similarity"] = min(1.0, source["similarity"] + 0.15)
                    source["cross_referenced"] = True

    sources.sort(key=lambda x: x["similarity"], reverse=True)

    # ── Step 6: Rerank ────────────────────────────────────────────────────
    if sources and settings.RERANKER_ENABLED:
        sources = await rerank(question, sources, top_k)
        logger.info("Reranked to %d sources", len(sources))
    else:
        sources = sources[:top_k]

    log_matches = log_matches[:top_k]
    concept_matches = concept_matches[:top_k]

    # ── Step 7: Load codebase summary ────────────────────────────────────
    codebase_summary = await _load_codebase_summary(session, repo_ids)

    # ── Step 8: LLM synthesis ─────────────────────────────────────────────
    has_context = bool(sources or log_matches or concept_matches or graph_context)

    if use_llm and has_context:
        combined_context = sources.copy()
        for lm in log_matches:
            combined_context.append({
                "file_path": f"[LOG: {lm['service_name']}]",
                "content": (
                    f"[{lm['level'] or 'LOG'}] "
                    f"{lm['timestamp'] or 'no-timestamp'}: {lm['message']}"
                ),
                "start_line": lm.get("line_reference", 0) or 0,
                "end_line": lm.get("line_reference", 0) or 0,
                "language": "log",
                "commit_sha": "n/a",
                "similarity": lm["similarity"],
            })
        # Inject LogConcepts as aggregated pattern context
        for cm in concept_matches:
            combined_context.append({
                "file_path": f"[LOG PATTERN: {cm['service_name']}]",
                "content": (
                    f"Pattern: {cm['template']}\n"
                    f"Occurrences: {cm['total_occurrences']} | "
                    f"Severity: {cm['severity']} | Trend: {cm['trend']}"
                ),
                "start_line": 0,
                "end_line": 0,
                "language": "log_concept",
                "commit_sha": "n/a",
                "similarity": cm["similarity"],
            })
        # Inject graph edges as relational context
        for edge in graph_context[:5]:
            combined_context.append({
                "file_path": f"[GRAPH EDGE: {edge['edge_type']}]",
                "content": (
                    f"{edge['source_entity']} --[{edge['edge_type']}]--> "
                    f"{edge['target_entity']} "
                    f"(confidence={edge.get('confidence', 0):.2f}, "
                    f"level={edge.get('promotion_level', 'unknown')})"
                ),
                "start_line": 0,
                "end_line": 0,
                "language": "graph",
                "commit_sha": "n/a",
                "similarity": edge.get("confidence", 0.5),
            })

        logger.info(
            "Sending %d code + %d log + %d concepts + %d graph to LLM",
            len(sources), len(log_matches), len(concept_matches), len(graph_context),
        )
        try:
            answer = await generate(
                question,
                combined_context,
                conversation_history=conversation_history,
                codebase_summary=codebase_summary,
            )
        except Exception as llm_exc:
            logger.warning("LLM synthesis failed (%s) — returning raw results", llm_exc)
            answer = (
                f"⚠️ LLM synthesis unavailable: {llm_exc}\n\n"
                f"Raw results: {len(sources)} code section(s), "
                f"{len(log_matches)} log match(es), "
                f"{len(concept_matches)} log pattern(s) retrieved. See sources below."
            )

    elif has_context:
        answer = (
            f"Found {len(sources)} relevant code sections, "
            f"{len(log_matches)} log entries, "
            f"{len(concept_matches)} log patterns, "
            f"{len(graph_context)} graph edges. "
            f"LLM synthesis is disabled — showing raw matches."
        )
    else:
        answer = (
            "No relevant code chunks or log entries found above the similarity "
            "threshold. Try ingesting a repository first (POST /api/ingest), "
            "or lower RAG_SIMILARITY_THRESHOLD in .env."
        )

    return {
        "query": question,
        "answer": answer,
        "sources": sources,
        "log_matches": log_matches,
        "concept_matches": concept_matches,
        "graph_context": graph_context,
        "metadata": {
            "chunks_retrieved": len(sources),
            "logs_retrieved": len(log_matches),
            "concepts_retrieved": len(concept_matches),
            "graph_edges_retrieved": len(graph_context),
            "similarity_threshold": similarity_threshold,
            "llm_enabled": use_llm,
            "hyde_used": use_llm and settings.HYDE_ENABLED,
            "reranker_used": settings.RERANKER_ENABLED and bool(sources),
            "codebase_summary_injected": codebase_summary is not None,
            "query_plan": plan_to_metadata(plan) if plan else None,
        },
    }


async def stream_query_codebase(
    question: str,
    session: AsyncSession,
    *,
    top_k: int = 5,
    similarity_threshold: float | None = None,
    conversation_history: list[dict] | None = None,
    repo_ids: list[uuid.UUID] | None = None,
):
    """Streaming variant of query_codebase.

    Yields NDJSON lines:
      {"type": "metadata", "data": {sources, log_matches, metadata}}
      {"type": "token",    "data": "<text chunk>"}
      {"type": "error",    "data": "<message>"}
    """
    if similarity_threshold is None:
        similarity_threshold = settings.RAG_SIMILARITY_THRESHOLD

    search_text = await _hyde_query(question) if (settings.HYDE_ENABLED and settings.LLM_AVAILABLE) else question
    query_embedding = await embed_text(search_text)
    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    fetch_k = settings.RERANKER_CANDIDATES if settings.RERANKER_ENABLED else top_k
    repo_clause = _repo_filter_clause(repo_ids)

    code_sql = text(f"""
        SELECT repo_id, file_path, chunk_content, start_line, end_line, language, commit_sha,
               1 - (embedding <=> CAST(:embedding AS halfvec)) AS similarity
        FROM code_chunks WHERE embedding IS NOT NULL {repo_clause}
        ORDER BY embedding <=> CAST(:embedding AS halfvec) LIMIT :fetch_k
    """)
    code_rows = (await session.execute(code_sql, {"embedding": embedding_str, "fetch_k": fetch_k})).fetchall()
    sources = [
        {"repo_id": str(row.repo_id) if row.repo_id else None, "file_path": row.file_path, "content": row.chunk_content,
         "start_line": row.start_line, "end_line": row.end_line, "language": row.language,
         "commit_sha": row.commit_sha, "similarity": float(row.similarity)}
        for row in code_rows if float(row.similarity) >= similarity_threshold
    ]

    log_sql = text(f"""
        SELECT service_name, timestamp, level, message, parsed_error, file_reference, line_reference,
               1 - (embedding <=> CAST(:embedding AS halfvec)) AS similarity
        FROM log_entries WHERE embedding IS NOT NULL {repo_clause}
        ORDER BY embedding <=> CAST(:embedding AS halfvec) LIMIT :fetch_k
    """)
    log_rows = (await session.execute(log_sql, {"embedding": embedding_str, "fetch_k": fetch_k})).fetchall()
    log_matches = [
        {"service_name": row.service_name, "timestamp": str(row.timestamp) if row.timestamp else None,
         "level": row.level, "message": row.message, "parsed_error": row.parsed_error,
         "file_reference": row.file_reference, "line_reference": row.line_reference,
         "similarity": float(row.similarity)}
        for row in log_rows if float(row.similarity) >= similarity_threshold
    ]

    for log_match in log_matches:
        if log_match.get("file_reference"):
            for source in sources:
                if log_match["file_reference"] in source["file_path"]:
                    source["similarity"] = min(1.0, source["similarity"] + 0.15)
                    source["cross_referenced"] = True

    sources.sort(key=lambda x: x["similarity"], reverse=True)

    if sources and settings.RERANKER_ENABLED:
        sources = await rerank(question, sources, top_k)
    else:
        sources = sources[:top_k]

    log_matches = log_matches[:top_k]
    codebase_summary = await _load_codebase_summary(session, repo_ids)

    metadata = {
        "chunks_retrieved": len(sources),
        "logs_retrieved": len(log_matches),
        "similarity_threshold": similarity_threshold,
        "llm_enabled": True,
        "hyde_used": settings.HYDE_ENABLED,
        "reranker_used": settings.RERANKER_ENABLED and bool(sources),
        "codebase_summary_injected": codebase_summary is not None,
    }

    yield json.dumps({"type": "metadata", "data": {"sources": sources, "log_matches": log_matches, "metadata": metadata}}) + "\n"

    has_context = bool(sources or log_matches)
    if not has_context:
        yield json.dumps({"type": "token", "data": "No relevant code chunks or log entries found."}) + "\n"
        return

    combined_context = sources.copy()
    for lm in log_matches:
        combined_context.append({
            "file_path": f"[LOG: {lm['service_name']}]",
            "content": f"[{lm['level'] or 'LOG'}] {lm['timestamp'] or 'no-timestamp'}: {lm['message']}",
            "start_line": lm.get("line_reference", 0) or 0,
            "end_line": lm.get("line_reference", 0) or 0,
            "language": "log",
            "commit_sha": "n/a",
            "similarity": lm["similarity"],
        })

    try:
        async for chunk in generate_stream(
            question, combined_context,
            conversation_history=conversation_history,
            codebase_summary=codebase_summary,
        ):
            yield json.dumps({"type": "token", "data": chunk}) + "\n"
    except Exception as llm_exc:
        yield json.dumps({"type": "error", "data": f"LLM streaming error: {llm_exc}"}) + "\n"
