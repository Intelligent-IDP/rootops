"""Tests for the Breadth dependency graph roll-up."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.services.breadth_graph import (
    _breadth_llm_options,
    _dependency_type,
    _persist_flowgraph_snapshot,
    roll_up,
)

FIXTURE = Path(__file__).parent / "fixtures" / "breadth_shop.flowgraph.json"


def _shop() -> dict:
    return json.loads(FIXTURE.read_text())


def _edges(graph) -> set[tuple[str, str, str]]:
    return {(e.source.name, e.edge_type, e.target.name) for e in graph.edges}


def test_rolls_functions_up_to_services():
    g = roll_up(_shop())
    edges = _edges(g)
    assert ("gateway", "calls", "orders") in edges
    assert ("orders", "calls", "payments") in edges
    assert ("payments", "calls", "api.stripe.com") in edges
    assert ("notifier", "calls", "api.sendgrid.com") in edges


def test_data_and_messaging_edges_point_from_the_dependent():
    edges = _edges(roll_up(_shop()))
    assert ("orders", "writes_to", "postgresql") in edges
    # Breadth draws reads as store -> reader; RootOps stores reader -> store.
    assert ("orders", "reads_from", "postgresql") in edges
    assert ("orders", "publishes_to", "order-created") in edges
    assert ("notifier", "subscribes_to", "order-created") in edges


def test_entity_types_and_aliases():
    g = roll_up(_shop())
    assert g.entities["orders"].entity_type == "Service"
    assert g.entities["postgresql"].entity_type == "Database"
    assert g.entities["order-created"].entity_type == "Topic"
    assert g.entities["kafka"].entity_type == "Queue"
    assert g.entities["api.stripe.com"].entity_type == "API"
    # Kubernetes and log names resolve to the same entity.
    assert "k8s:shop/Deployment/orders" in g.entities["orders"].aliases
    assert "service:orders" in g.entities["orders"].aliases


def test_in_process_state_is_not_an_entity():
    g = roll_up(_shop())
    assert "PRICE_CACHE" not in g.entities
    assert "BLOCKLIST" not in g.entities


def test_no_self_edges_and_edges_are_merged():
    g = roll_up(_shop())
    keys = [(e.source.name, e.target.name, e.edge_type) for e in g.edges]
    assert len(keys) == len(set(keys))
    assert all(e.source.name != e.target.name for e in g.edges)
    gateway_orders = next(
        e for e in g.edges if (e.source.name, e.target.name, e.edge_type) == ("gateway", "orders", "calls")
    )
    assert gateway_orders.count >= 2


def test_dependency_types_for_dashboard():
    by_key = {(e.source.name, e.edge_type, e.target.name): e for e in roll_up(_shop()).edges}
    assert _dependency_type(by_key[("orders", "calls", "payments")]) == "http"
    assert _dependency_type(by_key[("orders", "publishes_to", "order-created")]) == "event"
    assert _dependency_type(by_key[("orders", "writes_to", "postgresql")]) is None


def test_empty_graph():
    g = roll_up({"version": "1.0", "nodes": [], "edges": []})
    assert g.entities == {} and g.edges == []


@pytest.mark.anyio
async def test_persists_full_breadth_flowgraph():
    import uuid

    from unittest.mock import AsyncMock, MagicMock

    from app.models.breadth_graph_snapshot import BreadthGraphSnapshot

    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    repo_id = uuid.uuid4()
    flowgraph = {"version": "1.0", "nodes": [{"id": "function:main"}], "edges": []}

    await _persist_flowgraph_snapshot(session, flowgraph, repo_id)

    session.get.assert_awaited_once_with(BreadthGraphSnapshot, repo_id)
    stored = session.add.call_args.args[0]
    assert stored.repo_id == repo_id
    assert stored.flowgraph == flowgraph


def test_breadth_llm_uses_selected_ollama_backend():
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "BREADTH_LLM_ENABLED": True,
        "LLM_BACKEND": "ollama",
        "OLLAMA_MODEL": "qwen2.5-coder",
        "OLLAMA_BASE_URL": "http://ollama:11434",
    })
    args, env = _breadth_llm_options(settings)

    assert args == ["--llm"]
    assert env == {
        "BREADTH_LLM_PROVIDER": "ollama",
        "BREADTH_LLM_MODEL": "qwen2.5-coder",
        "BREADTH_LLM_BASE_URL": "http://ollama:11434",
    }


def test_breadth_llm_forwards_selected_remote_provider_key():
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "BREADTH_LLM_ENABLED": True,
        "LLM_BACKEND": "gemini",
        "GEMINI_API_KEY": "test-key",
        "GEMINI_MODEL": "gemini-test",
    })
    args, env = _breadth_llm_options(settings)

    assert args == ["--llm"]
    assert env["BREADTH_LLM_PROVIDER"] == "gemini"
    assert env["BREADTH_LLM_MODEL"] == "gemini-test"
    assert env["GEMINI_API_KEY"] == "test-key"


def test_breadth_llm_skips_unavailable_and_unsupported_backends():
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "BREADTH_LLM_ENABLED": True,
        "LLM_BACKEND": "openai",
        "OPENAI_API_KEY": "",
    })
    assert _breadth_llm_options(settings) == ([], {})

    bedrock = settings.model_copy(update={"LLM_BACKEND": "bedrock"})
    assert _breadth_llm_options(bedrock) == ([], {})


@pytest.mark.anyio
async def test_scan_passes_selected_llm_to_breadth_cli():
    from app.services import breadth_graph
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "BREADTH_LLM_ENABLED": True,
        "LLM_BACKEND": "ollama",
        "OLLAMA_MODEL": "qwen2.5-coder",
        "OLLAMA_BASE_URL": "http://ollama:11434",
    })
    flowgraph = {"version": "1.0", "nodes": [], "edges": []}

    class CompletedScan:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*args, **_kwargs):
        assert "stdout" in _kwargs and "stderr" in _kwargs
        assert "BREADTH_LLM_PROVIDER" in _kwargs["env"]
        output_path = args[args.index("-o") + 1]
        Path(output_path).write_text(json.dumps(flowgraph), encoding="utf-8")
        return CompletedScan()

    with (
        patch.object(breadth_graph, "get_settings", return_value=settings),
        patch.object(breadth_graph, "breadth_command", return_value=["breadth"]),
        patch.object(
            breadth_graph.asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=fake_create_subprocess_exec,
        ) as create_process,
    ):
        result = await breadth_graph.scan_repository("/repo")

    assert result == flowgraph
    call = create_process.await_args
    assert call is not None
    assert "--llm" in call.args
    assert call.kwargs["env"]["BREADTH_LLM_PROVIDER"] == "ollama"
    assert call.kwargs["env"]["BREADTH_LLM_MODEL"] == "qwen2.5-coder"


@pytest.mark.anyio
async def test_scan_retries_without_llm_when_provider_fails():
    from app.services import breadth_graph
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "BREADTH_LLM_ENABLED": True,
        "LLM_BACKEND": "ollama",
        "OLLAMA_MODEL": "qwen2.5-coder",
        "OLLAMA_BASE_URL": "http://ollama:11434",
    })
    flowgraph = {"version": "1.0", "nodes": [], "edges": []}
    calls = 0

    class FailedScan:
        returncode = 1

        async def communicate(self):
            return b"", b"LLM provider unavailable"

    class CompletedScan:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*args, **_kwargs):
        assert "stdout" in _kwargs and "stderr" in _kwargs
        assert "env" in _kwargs
        nonlocal calls
        calls += 1
        if "--llm" in args:
            return FailedScan()
        output_path = args[args.index("-o") + 1]
        Path(output_path).write_text(json.dumps(flowgraph), encoding="utf-8")
        return CompletedScan()

    with (
        patch.object(breadth_graph, "get_settings", return_value=settings),
        patch.object(breadth_graph, "breadth_command", return_value=["breadth"]),
        patch.object(
            breadth_graph.asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=fake_create_subprocess_exec,
        ) as create_process,
    ):
        result = await breadth_graph.scan_repository("/repo")

    assert result == flowgraph
    assert calls == 2
    call = create_process.await_args
    assert call is not None
    assert "--llm" not in call.args


# ── Ingestion wiring ──────────────────────────────────────────────

def _settings(source: str):
    from app.config import get_settings

    return get_settings().model_copy(update={"DEP_GRAPH_SOURCE": source})


@pytest.mark.anyio
async def test_auto_falls_back_to_regex_when_breadth_missing(mock_session):
    from app.services import git_ingestor

    with patch.object(git_ingestor, "settings", _settings("auto")), \
         patch("app.services.breadth_graph.scan_repository", AsyncMock(return_value=None)), \
         patch("app.services.dep_extractor.extract_and_persist_dependencies", AsyncMock(return_value=4)) as regex:
        n = await git_ingestor._extract_dependency_graph(
            mock_session, repo_id=uuid.uuid4(), repo_name="shop", repo_path="/x", file_contents=[],
        )
    assert n == 4
    regex.assert_awaited_once()


@pytest.mark.anyio
async def test_breadth_is_used_when_available(mock_session):
    from app.services import git_ingestor

    with patch.object(git_ingestor, "settings", _settings("auto")), \
         patch("app.services.breadth_graph.scan_repository", AsyncMock(return_value=_shop())), \
         patch("app.services.breadth_graph.import_flowgraph", AsyncMock(return_value={"dependencies": 7})), \
         patch("app.services.dep_extractor.extract_and_persist_dependencies", AsyncMock()) as regex:
        n = await git_ingestor._extract_dependency_graph(
            mock_session, repo_id=uuid.uuid4(), repo_name="shop", repo_path="/x", file_contents=[],
        )
    assert n == 7
    regex.assert_not_awaited()


@pytest.mark.anyio
async def test_breadth_only_mode_fails_loudly(mock_session):
    from app.services import git_ingestor

    with patch.object(git_ingestor, "settings", _settings("breadth")), \
         patch("app.services.breadth_graph.scan_repository", AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError):
            await git_ingestor._extract_dependency_graph(
                mock_session, repo_id=uuid.uuid4(), repo_name="shop", repo_path="/x", file_contents=[],
            )
