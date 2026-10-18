"""
RootOps V2 — Query API Router

Endpoints for querying the Semantic Engine's semantic memory.
This is the primary interface for developers to interact with RootOps.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.rag_engine import query_codebase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/query", tags=["query"])


class ConversationTurn(BaseModel):
    """A single turn in the conversation history."""
    role: str = Field(..., description="'user' or 'assistant'")
    content: str = Field(..., description="The message text.")


class QueryRequest(BaseModel):
    """Request body for querying the codebase."""
    question: str = Field(
        ...,
        description="Natural language question about the codebase.",
        examples=["What does the authentication middleware do?"],
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of most relevant code chunks to retrieve.",
    )
    use_llm: bool = Field(
        default=True,
        description=(
            "Whether to synthesize results via LLM. "
            "Set to false for raw vector search results."
        ),
    )
    conversation_history: list[ConversationTurn] = Field(
        default_factory=list,
        description=(
            "Prior conversation turns sent to the LLM for context. "
            "The LLM can reference earlier questions and answers."
        ),
    )
    repo_ids: list[str] | None = Field(
        default=None,
        description=(
            "Optional list of repository UUIDs to scope the search to. "
            "Omit for global search across all ingested repositories."
        ),
    )


class SourceChunk(BaseModel):
    """A relevant code chunk returned as a source."""
    file_path: str
    content: str
    start_line: int
    end_line: int
    language: str | None
    commit_sha: str
    similarity: float
    cross_referenced: bool = False


class LogMatch(BaseModel):
    """A relevant log entry returned as a match."""
    service_name: str
    timestamp: str | None
    level: str | None
    message: str
    parsed_error: str | None
    file_reference: str | None
    line_reference: int | None
    similarity: float


class QueryMetadata(BaseModel):
    """Metadata about the query execution."""
    chunks_retrieved: int
    logs_retrieved: int = 0
    similarity_threshold: float
    llm_enabled: bool
    hyde_used: bool = False
    reranker_used: bool = False
    codebase_summary_injected: bool = False


class QueryResponse(BaseModel):
    """Response from a codebase query."""
    query: str
    answer: str
    sources: list[SourceChunk]
    log_matches: list[LogMatch] = []
    metadata: QueryMetadata


@router.post("", response_model=QueryResponse)
async def query(
    request: QueryRequest,
    session: AsyncSession = Depends(get_db),
):
    """Query the Semantic Engine about the ingested codebase.

    Uses Hybrid RAG: vector similarity search (pgvector) across both
    code chunks and log entries, cross-correlates them, then synthesizes
    a response via the configured LLM backend.
    """
    history = [t.model_dump() for t in request.conversation_history]
    repo_ids = [uuid.UUID(r) for r in request.repo_ids] if request.repo_ids else None
    result = await query_codebase(
        question=request.question,
        session=session,
        top_k=request.top_k,
        use_llm=request.use_llm,
        conversation_history=history or None,
        repo_ids=repo_ids,
    )
    return QueryResponse(**result)


@router.post("/stream")
async def query_stream(
    request: QueryRequest,
    session: AsyncSession = Depends(get_db),
):
    from fastapi.responses import StreamingResponse

    from app.services.rag_engine import stream_query_codebase
    history = [t.model_dump() for t in request.conversation_history]
    repo_ids = [uuid.UUID(r) for r in request.repo_ids] if request.repo_ids else None

    return StreamingResponse(
        stream_query_codebase(
            question=request.question,
            session=session,
            top_k=request.top_k,
            conversation_history=history or None,
            repo_ids=repo_ids,
        ),
        media_type="application/x-ndjson"
    )

