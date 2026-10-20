"""
RootOps — Service-layer unit tests

Tests pure functions and service logic that do NOT require a live DB,
LLM, or embedding model.  External I/O is mocked where necessary.

Coverage:
  - healer: _compute_confidence, _trust_ladder, _extract_code_blocks, _extract_section
  - llm_backend: format_context, build_system_prompt
  - embedding: validate_embedding_dimension (dimension mismatch detection)
  - rag_engine: _repo_filter_clause
  - config: RAM tier selection logic
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest


# ── Healer: pure functions ────────────────────────────────────────

class TestComputeConfidence:
    def _call(self, sim: float, n_related: int, text: str) -> float:
        from app.services.healer import _compute_confidence
        return _compute_confidence(sim, n_related, text)

    def test_perfect_signal_returns_high_confidence(self):
        long_text_with_code = "root cause: " + "x" * 300 + "\n```python\nfix_code()\n```"
        score = self._call(1.0, 5, long_text_with_code)
        assert score >= 0.8, f"Expected ≥0.8, got {score}"

    def test_no_signal_returns_low_confidence(self):
        score = self._call(0.0, 0, "")
        assert score < 0.3, f"Expected <0.3, got {score}"

    def test_score_clamped_to_one(self):
        text = "```python\nfixed()\n```\n" + "a" * 1000
        score = self._call(1.0, 100, text)
        assert score <= 1.0

    def test_score_never_negative(self):
        score = self._call(0.0, 0, "")
        assert score >= 0.0


class TestGeminiClient:
    @pytest.mark.anyio
    async def test_generate_uses_async_google_client(self, monkeypatch):
        generated: list[dict] = []

        async def generate_content(**kwargs):
            generated.append(kwargs)
            await asyncio.sleep(0)
            return SimpleNamespace(text="Gemini test response")

        class FakeClient:
            def __init__(self, api_key):
                assert api_key == "test-key"
                self.aio = SimpleNamespace(
                    models=SimpleNamespace(generate_content=generate_content),
                )

        class FakePart:
            @staticmethod
            def from_text(*, text):
                return text

        class FakeContent:
            def __init__(self, *, role, parts):
                self.role = role
                self.parts = parts

        class FakeGenerateConfig:
            def __init__(self, **kwargs):
                self.options = kwargs

        google_module = ModuleType("google")
        genai_module = ModuleType("google.genai")
        types_module = ModuleType("google.genai.types")
        genai_module.Client = FakeClient
        genai_module.types = types_module
        types_module.Content = FakeContent
        types_module.Part = FakePart
        types_module.GenerateContentConfig = FakeGenerateConfig
        google_module.genai = genai_module
        monkeypatch.setitem(sys.modules, "google", google_module)
        monkeypatch.setitem(sys.modules, "google.genai", genai_module)
        monkeypatch.setitem(sys.modules, "google.genai.types", types_module)

        from app.services import gemini_client

        monkeypatch.setattr(gemini_client.settings, "GEMINI_API_KEY", "test-key")
        response = await gemini_client.generate_gemini("prompt", "system")

        assert response == "Gemini test response"
        assert len(generated) == 1


class TestTrustLadder:
    def _call(self, confidence: float, blast: str):
        from app.services.healer import _trust_ladder
        return _trust_ladder(confidence, blast)

    def test_high_confidence_low_blast_eligible_for_auto_apply(self):
        requires_approval, auto_eligible = self._call(0.95, "low")
        assert auto_eligible is True
        assert requires_approval is False

    def test_high_blast_always_requires_approval(self):
        _, auto = self._call(0.99, "high")
        assert auto is False

    def test_medium_blast_requires_approval(self):
        requires, auto = self._call(0.99, "medium")
        assert requires is True
        assert auto is False

    def test_low_confidence_requires_approval(self):
        requires, auto = self._call(0.10, "low")
        assert requires is True
        assert auto is False


class TestExtractCodeBlocks:
    def _call(self, text: str) -> str:
        from app.services.healer import _extract_code_blocks
        return _extract_code_blocks(text)

    def test_extracts_python_block(self):
        text = "Here is the fix:\n```python\ndef fix():\n    pass\n```"
        result = self._call(text)
        assert "def fix():" in result

    def test_empty_when_no_block(self):
        assert self._call("No code here") == ""

    def test_multiple_blocks_joined(self):
        text = "```python\na=1\n```\n\n```python\nb=2\n```"
        result = self._call(text)
        assert "a=1" in result
        assert "b=2" in result


class TestExtractSection:
    def _call(self, text: str, heading: str) -> str:
        from app.services.healer import _extract_section
        return _extract_section(text, heading)

    def test_extracts_named_section(self):
        text = "**Root Cause**: The service failed.\n\n**Rollback Plan**: Revert commit abc."
        assert "service failed" in self._call(text, "Root Cause")

    def test_returns_empty_when_section_missing(self):
        assert self._call("No sections here", "Rollback Plan") == ""


# ── LLM Backend: format_context ──────────────────────────────────

class TestFormatContext:
    def _call(self, chunks: list) -> str:
        from app.services.llm_backend import format_context
        return format_context(chunks)

    def test_empty_chunks_returns_placeholder(self):
        result = self._call([])
        assert "No relevant code context" in result

    def test_formats_file_path_and_lines(self):
        chunks = [{
            "file_path": "src/auth.py",
            "content": "def login(): pass",
            "start_line": 10,
            "end_line": 15,
            "language": "python",
            "similarity": 0.87,
        }]
        result = self._call(chunks)
        assert "src/auth.py" in result
        assert "lines 10-15" in result

    def test_shows_rerank_score_when_present(self):
        chunks = [{
            "file_path": "app.py",
            "content": "pass",
            "start_line": 1,
            "end_line": 1,
            "language": "python",
            "similarity": 0.5,
            "rerank_score": 0.923,
        }]
        result = self._call(chunks)
        assert "rerank: 0.923" in result

    def test_truncates_long_content(self):
        long_content = "x" * 10_000
        chunks = [{
            "file_path": "big.py",
            "content": long_content,
            "start_line": 1,
            "end_line": 100,
            "language": "python",
            "similarity": 0.7,
        }]
        result = self._call(chunks)
        # Should be truncated — result must be much shorter than input
        assert len(result) < len(long_content)


class TestBuildSystemPrompt:
    def test_base_prompt_returned_when_no_summary(self):
        from app.services.llm_backend import build_system_prompt
        prompt = build_system_prompt(None)
        assert "RootOps" in prompt
        assert "## Codebase Architecture" not in prompt

    def test_summary_injected_when_provided(self):
        from app.services.llm_backend import build_system_prompt
        prompt = build_system_prompt("The service handles payments.")
        assert "## Codebase Architecture" in prompt
        assert "payments" in prompt


# ── RAG Engine: SQL helpers ──────────────────────────────────────

class TestRepoFilterClause:
    def _call(self, repo_ids, table=""):
        from app.services.rag_engine import _repo_filter_clause
        import uuid
        ids = [uuid.UUID(r) if isinstance(r, str) else r for r in repo_ids] if repo_ids else repo_ids
        return _repo_filter_clause(ids, table)

    def test_empty_returns_empty_string(self):
        assert self._call(None) == ""
        assert self._call([]) == ""

    def test_single_id_produces_in_clause(self):
        import uuid
        uid = uuid.uuid4()
        result = self._call([uid])
        assert "IN" in result
        assert str(uid) in result

    def test_table_prefix_applied(self):
        import uuid
        uid = uuid.uuid4()
        result = self._call([uid], table="code_chunks")
        assert "code_chunks.repo_id" in result


# ── Embedding: dimension validation ──────────────────────────────

class TestValidateEmbeddingDimension:
    def test_raises_on_mismatch(self):
        from unittest.mock import MagicMock, patch
        from app.services.embedding import validate_embedding_dimension

        mock_model = MagicMock()
        mock_model.get_sentence_embedding_dimension.return_value = 384  # wrong

        with patch("app.services.embedding._get_embed_model", return_value=mock_model):
            with pytest.raises(RuntimeError, match="dimension mismatch"):
                validate_embedding_dimension("fake-model", expected_dim=768)

    def test_passes_on_correct_dimension(self):
        from unittest.mock import MagicMock, patch
        from app.services.embedding import validate_embedding_dimension

        mock_model = MagicMock()
        mock_model.get_sentence_embedding_dimension.return_value = 768

        with patch("app.services.embedding._get_embed_model", return_value=mock_model):
            # Should not raise
            validate_embedding_dimension("fake-model", expected_dim=768)


# ── Config: RAM tier defaults ─────────────────────────────────────

class TestConfigRamTier:
    def test_large_ram_uses_ollama(self):
        """≥16 GB should default to Ollama (verifiable on CI runners)."""
        import app.config as cfg
        # CI runners have ≥16 GB, so the default tier should be ollama
        assert cfg._T["llm"] == "ollama"

    @pytest.mark.skipif(
        True,  # Module-level RAM detection can't be reliably mocked via reload
        reason="RAM tier is computed at import time; test only valid on ≤8GB machines",
    )
    def test_small_ram_uses_openai(self):
        """≤8 GB should default to openai (Ollama is too slow)."""
        import app.config as cfg
        assert cfg._T["llm"] == "openai"
