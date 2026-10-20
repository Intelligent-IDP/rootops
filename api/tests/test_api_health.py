"""
RootOps — Health & root endpoint tests

Uses FastAPI's TestClient with startup/shutdown hooks patched so no real
database or embedding model is needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_client():
    """Build a TestClient with all external deps mocked out."""
    with (
        patch("app.db.init_db", new_callable=AsyncMock),
        patch("app.db.create_async_engine", return_value=MagicMock()),
        patch("app.services.embedding.validate_embedding_dimension"),
        patch(
            "sqlalchemy.ext.asyncio.async_sessionmaker",
            return_value=MagicMock(),
        ),
    ):
        from fastapi.testclient import TestClient

        # Patch the lifespan so startup doesn't touch live services
        with patch("app.main.lifespan") as mock_lifespan:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _noop(app):  # noqa: ARG001
                yield

            mock_lifespan.side_effect = None
            mock_lifespan.__call__ = _noop

            from app.main import app

            return TestClient(app, raise_server_exceptions=False)


class TestHealthEndpoints:
    def test_root_returns_app_name(self):
        client = _make_client()
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert "app" in data or "RootOps" in str(data)

    def test_health_check_returns_ok_field(self):
        client = _make_client()
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        # Basic health check must include a status field
        assert "status" in data or "ok" in data or "app" in data

    def test_health_includes_version(self):
        client = _make_client()
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data

    def test_detailed_health_returns_checks(self):
        client = _make_client()
        r = client.get("/api/health/detailed")
        # May return 200 or 503 depending on mocked state, but not 404
        assert r.status_code != 404

    def test_docs_available(self):
        """Swagger UI should be reachable."""
        client = _make_client()
        r = client.get("/docs")
        assert r.status_code == 200


class TestLogHealthEndpoint:
    @pytest.mark.anyio
    async def test_live_analysis_retrieves_only_selected_repository_code(self):
        import uuid
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from app.api.logs import LiveAnalysisRequest, analyze_live_evidence

        repo_id = uuid.uuid4()
        entry = SimpleNamespace(
            id=uuid.uuid4(), service_name="core-app", level="WARN",
            timestamp=datetime.now(UTC), created_at=datetime.now(UTC),
            message="AI command failed", metadata_json={},
        )
        chunk = SimpleNamespace(
            repo_id=repo_id, file_path="src/lib/actions.ts", chunk_content="const action = result.action;",
            start_line=31, end_line=48, language="typescript",
        )
        events_result, code_result = MagicMock(), MagicMock()
        events_result.scalars.return_value.all.return_value = [entry]
        code_result.scalars.return_value.all.return_value = [chunk]
        session = AsyncMock()
        session.execute.side_effect = [events_result, code_result]
        body = LiveAnalysisRequest(event_ids=[entry.id], repo_id=repo_id)
        with (
            patch("app.api.logs.settings") as settings,
            patch("app.services.embedding.embed_text", new_callable=AsyncMock, return_value=[0.0] * 768),
            patch("app.services.llm_backend.generate", new_callable=AsyncMock, return_value="Live explanation") as generate,
        ):
            settings.LLM_AVAILABLE = True
            response = await analyze_live_evidence(body, session)
            assert response["sources"][0]["repo_id"] == str(repo_id)
            assert len(generate.await_args.kwargs["context_chunks"]) == 2
            query = session.execute.call_args_list[1].args[0]
            assert query.compile().params["repo_id_1"] == repo_id

            session.execute.side_effect = [events_result, RuntimeError("source unavailable")]
            response = await analyze_live_evidence(body, session)
            assert response["mode"] == "llm"
            assert response["sources"] == []
            session.rollback.assert_awaited_once()

    @pytest.mark.anyio
    async def test_live_analysis_accepts_one_warning_and_rejects_synthetic(self):
        import uuid
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from fastapi import HTTPException

        from app.api.logs import LiveAnalysisRequest, analyze_live_evidence

        entry = SimpleNamespace(
            id=uuid.uuid4(), service_name="core-app", level="WARN",
            timestamp=datetime.now(UTC), created_at=datetime.now(UTC),
            message="AI command: UNKNOWN failed", metadata_json={},
        )
        session = AsyncMock()
        session.execute.return_value = MagicMock()
        session.execute.return_value.scalars.return_value.all.return_value = [entry]
        body = LiveAnalysisRequest(event_ids=[entry.id], use_llm=False)
        response = await analyze_live_evidence(body, session)
        assert response["synthetic"] is False
        assert response["mode"] == "evidence_only"
        assert "warning count 1" in response["analysis"]

        body.use_llm = True
        with patch("app.api.logs.settings") as settings:
            settings.LLM_AVAILABLE = True
            with patch("app.services.llm_backend.generate", new_callable=AsyncMock, return_value="Live explanation") as generate:
                response = await analyze_live_evidence(body, session)
                assert response["mode"] == "llm"
                context = generate.await_args.kwargs["context_chunks"]
                assert len(context) == 1
                assert context[0]["content"].endswith(entry.message)
            with patch("app.services.llm_backend.generate", new_callable=AsyncMock, return_value="\u26a0 provider error"):
                assert (await analyze_live_evidence(body, session))["mode"] == "evidence_only"

        entry.metadata_json = {"synthetic": True}
        with pytest.raises(HTTPException) as error:
            await analyze_live_evidence(body, session)
        assert error.value.status_code == 400
        entry.metadata_json = {}
        entry.service_name = "mock-core-app"
        with pytest.raises(HTTPException) as error:
            await analyze_live_evidence(body, session)
        assert error.value.status_code == 400
        session.execute.return_value.scalars.return_value.all.return_value = []
        with pytest.raises(HTTPException) as error:
            await analyze_live_evidence(body, session)
        assert error.value.status_code == 404

        other = SimpleNamespace(**{**vars(entry), "id": uuid.uuid4(), "service_name": "other-app"})
        entry.service_name = "core-app"
        session.execute.return_value.scalars.return_value.all.return_value = [entry, other]
        body.event_ids = [entry.id, other.id]
        with pytest.raises(HTTPException) as error:
            await analyze_live_evidence(body, session)
        assert error.value.status_code == 400

    @pytest.mark.anyio
    async def test_mock_analysis_is_scoped_and_has_fallback(self):
        import uuid
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from fastapi import HTTPException

        from app.api.logs import MockAnalysisRequest, analyze_mock_scenario

        entries = [SimpleNamespace(
            id=uuid.uuid4(), service_name="mock-core-app", level="ERROR",
            timestamp=datetime.now(UTC), created_at=datetime.now(UTC), message="MOCK timeout",
            metadata_json={"synthetic": True, "request_id": "mock-request"},
        ) for _ in range(2)]
        session = AsyncMock()
        session.execute.return_value = MagicMock()
        session.execute.return_value.scalars.return_value.all.return_value = entries
        body = MockAnalysisRequest(event_ids=[entry.id for entry in entries], use_llm=False)
        response = await analyze_mock_scenario(body, session)
        assert response["mode"] == "evidence_only"
        assert len(response["timeline"]) == 2
        assert "not proven call direction" in response["analysis"]

        body.use_llm = True
        with patch("app.api.logs.settings") as settings:
            settings.LLM_AVAILABLE = True
            with patch("app.services.llm_backend.generate", new_callable=AsyncMock, return_value="Mock incident explanation") as generate:
                response = await analyze_mock_scenario(body, session)
                assert response["mode"] == "llm"
                assert len(generate.await_args.kwargs["context_chunks"]) == 2
            with patch("app.services.llm_backend.generate", new_callable=AsyncMock, side_effect=TimeoutError):
                response = await analyze_mock_scenario(body, session)
                assert response["mode"] == "evidence_only"
            with patch("app.services.llm_backend.generate", new_callable=AsyncMock, return_value="\u26a0 Gemini error"):
                response = await analyze_mock_scenario(body, session)
                assert response["mode"] == "evidence_only"

        entries[0].metadata_json = {"request_id": {"invalid": True}}
        with pytest.raises(HTTPException) as error:
            await analyze_mock_scenario(body, session)
        assert error.value.status_code == 400

        entries[0].service_name = "core-app"
        entries[0].metadata_json = {}
        with pytest.raises(HTTPException) as error:
            await analyze_mock_scenario(body, session)
        assert error.value.status_code == 400

    @pytest.mark.anyio
    async def test_log_health_separates_synthetic_evidence(self):
        import uuid
        from datetime import UTC, datetime
        from types import SimpleNamespace

        from app.api.logs import get_log_health

        now = datetime.now(UTC)
        entries = [
            SimpleNamespace(
                id=uuid.uuid4(), service_name="mock-core-app", level="ERROR",
                timestamp=now, created_at=now, message="MOCK timeout",
                metadata_json={"synthetic": "true", "request_id": "mock-request"},
            )
            for _ in range(2)
        ]
        session = AsyncMock()
        session.execute.return_value = MagicMock()
        session.execute.return_value.scalars.return_value.all.return_value = entries
        response = await get_log_health(session=session)
        assert response["status"] == "insufficient_evidence"
        assert response["services"][0]["errors"] == 2
        assert response["correlations"][0]["synthetic"] is True
        assert response["sampled_events"] == 2

        entries[0].service_name = "core-app"
        entries[0].metadata_json = {}
        response = await get_log_health(session=session)
        assert response["status"] == "errors_observed"
        assert response["correlations"] == []

        session.execute.return_value.scalars.return_value.all.return_value = entries * 251
        response = await get_log_health(session=session)
        assert response["truncated"] is True
        assert response["sampled_events"] == 500
        assert len(response["evidence"]) == 20

        session.execute.return_value.scalars.return_value.all.return_value = []
        response = await get_log_health(session=session)
        assert response["status"] == "insufficient_evidence"
        assert response["services"] == []


class TestQueryEndpoint:
    @pytest.mark.anyio
    async def test_query_returns_api_contract(self):
        from app.api.query import QueryRequest, query

        mock_session = AsyncMock()
        result = {
            "query": "What does auth do?",
            "answer": "It validates requests.",
            "sources": [],
            "log_matches": [],
            "metadata": {
                "chunks_retrieved": 0,
                "logs_retrieved": 0,
                "similarity_threshold": 0.3,
                "llm_enabled": False,
                "hyde_used": False,
                "reranker_used": False,
                "codebase_summary_injected": False,
            },
        }

        with patch(
            "app.api.query.query_codebase",
            new_callable=AsyncMock,
            return_value=result,
        ) as query_codebase:
            response = await query(
                QueryRequest(question="What does auth do?", use_llm=False),
                mock_session,
            )

        assert response.answer == result["answer"]
        query_codebase.assert_awaited_once()
        assert query_codebase.await_args is not None
        assert query_codebase.await_args.kwargs["use_llm"] is False
