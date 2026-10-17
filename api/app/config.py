"""
RootOps — Application Configuration

Reads settings from environment variables (or .env file) using pydantic-settings.
All values have sensible defaults for local Docker Compose usage.

Embedding / ingestion settings are **auto-tuned** based on detected system RAM
(including Docker cgroup limits).  Override any value via env var if needed.
"""

import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── RAM auto-detection ───────────────────────────────────────────

def _detect_available_ram_gb() -> float:
    """Detect available RAM in GB, respecting Docker/cgroup memory limits.

    Checks (in order):
      1. cgroup v2 memory.max  (modern Docker / Kubernetes)
      2. cgroup v1 memory.limit_in_bytes  (older Docker)
      3. OS-level total physical memory  (bare metal / macOS)
      4. Fallback: 16 GB (don't restrict if detection fails)
    """
    # 1. cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            val = f.read().strip()
            if val != "max":
                return int(val) / (1024 ** 3)
    except (FileNotFoundError, ValueError, PermissionError):
        pass

    # 2. cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            val = int(f.read().strip())
            # Values near maxint mean "no limit"
            if val < 2 ** 62:
                return val / (1024 ** 3)
    except (FileNotFoundError, ValueError, PermissionError):
        pass

    # 3. POSIX (Linux / macOS bare metal)
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return (pages * page_size) / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        pass

    # 4. Fallback — assume plenty so we don't accidentally restrict
    return 16.0


DETECTED_RAM_GB: float = round(_detect_available_ram_gb(), 1)

# ── Auto-tune embedding defaults based on available RAM ──────────
# These become the *default* values in Settings.  Explicit env vars
# always take priority (pydantic-settings reads env first).
#
#   RAM tier     │ batch │ workers │ encode_bs │ max_chars │ max_seq │ summary │ deps │ LLM default
#   ─────────────┼───────┼─────────┼───────────┼───────────┼─────────┼─────────┼──────┼────────────
#   ≤ 4 GB       │   4   │    1    │     1     │    800    │   128   │  skip   │ skip │  openai *
#   4 – 8 GB     │   8   │    1    │     2     │   1200    │   256   │  skip   │ skip │  openai *
#   8 – 16 GB    │  32   │    2    │     8     │   2000    │   512   │   on    │  on  │  ollama
#   ≥ 16 GB      │  64   │    2    │    16     │   4000    │    0**  │   on    │  on  │  ollama
#
#   *  Ollama CPU-only is unusable on ≤8 GB (3-5 min/query). Default to
#      a cloud backend; if no API key is set, LLM features are simply OFF
#      and queries return raw vector search results.
#   ** 0 = use model's built-in max_seq_length (8192 for jina-v2)
#
#   To force Ollama anyway: set FORCE_OLLAMA=true (expect very slow queries).

if DETECTED_RAM_GB <= 4:
    _T = dict(batch=4,  workers=1, encode_bs=1,  chars=800,  seq=128, no_summary=True,  no_deps=True,  hyde=False, llm="openai", tier="≤4 GB (minimal)")
elif DETECTED_RAM_GB <= 8:
    _T = dict(batch=8,  workers=1, encode_bs=2,  chars=1200, seq=256, no_summary=True,  no_deps=True,  hyde=False, llm="openai", tier="4–8 GB (conservative)")
elif DETECTED_RAM_GB <= 16:
    _T = dict(batch=32, workers=2, encode_bs=8,  chars=2000, seq=512, no_summary=False, no_deps=False, hyde=True,  llm="ollama", tier="8–16 GB (standard)")
else:
    _T = dict(batch=64, workers=2, encode_bs=16, chars=4000, seq=0,   no_summary=False, no_deps=False, hyde=True,  llm="ollama", tier="≥16 GB (full)")

_RAM_TIER_LABEL: str = _T["tier"]


class Settings(BaseSettings):
    """Central configuration for the RootOps application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────
    APP_NAME: str = "RootOps"
    DEBUG: bool = False

    # ── Database ─────────────────────────────────────────────────
    DATABASE_URL: str = (
        "postgresql+asyncpg://rootops:rootops_secret@db:5432/rootops"
    )

    # ── Embedding ────────────────────────────────────────────────
    # jinaai/jina-embeddings-v2-base-code: trained on code + English text,
    # 8192-token context, 768-dim. Ideal for mixed code/log corpora.
    # If you change this, also update EMBEDDING_DIMENSION and run migration 0003.
    EMBEDDING_DIMENSION: int = 768
    EMBEDDING_MODEL_NAME: str = "jinaai/jina-embeddings-v2-base-code"

    # ── Reranker ─────────────────────────────────────────────────
    # Cross-encoder reranker: reads query + chunk together for much better
    # precision than bi-encoder similarity alone. Runs on CPU, ~5ms per chunk.
    RERANKER_ENABLED: bool = True
    RERANKER_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    # Retrieve this many candidates before reranking down to top_k.
    RERANKER_CANDIDATES: int = 20

    # ── HyDE (Hypothetical Document Embedding) ───────────────────
    # Before embedding a query, ask the LLM to generate a hypothetical code
    # answer, then embed THAT. Searching code-to-code beats question-to-code.
    # Auto-disabled on ≤8 GB RAM to avoid the extra Ollama round-trip.
    HYDE_ENABLED: bool = _T["hyde"]

    # ── Conversation history ─────────────────────────────────────
    # How many prior turns to include in each LLM call.
    CONVERSATION_HISTORY_TURNS: int = 5

    # ── LLM Backend ─────────────────────────────────────────────
    # Options: "ollama", "openai", "anthropic", "bedrock", "gemini"
    # On ≤8 GB RAM, defaults to "openai" (Ollama CPU is too slow).
    # On ≥8 GB, defaults to "ollama" (free, local).
    # Always overridable via env var LLM_BACKEND.
    LLM_BACKEND: str = _T["llm"]

    # ── Ollama (Local LLM — default, free, no API key) ───────────
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_MODEL: str = "llama3"

    # ── OpenAI ───────────────────────────────────────────────────
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"

    # ── Anthropic ────────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"

    # ── AWS Bedrock (requires AWS credentials / IAM role) ────────
    BEDROCK_MODEL_ID: str = "eu.anthropic.claude-sonnet-4-6"
    BEDROCK_REGION: str = "eu-west-1"
    AWS_REGION: str = "eu-west-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""

    # ── Google Gemini ────────────────────────────────────────────
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # ── OpenTelemetry Log Receiver (OTLP/HTTP) ─────────────────
    OTEL_LOGS_RECEIVER_ENABLED: bool = True

    # ── Log Filtering ────────────────────────────────────────────
    # Severity floor: only ingest logs at this level or above.
    # Options: DEBUG, INFO, WARN, ERROR, FATAL  (default: WARN)
    LOG_MIN_SEVERITY: str = "WARN"

    # Service allowlist: comma-separated. Empty = accept all services.
    LOG_ALLOWED_SERVICES: str = ""

    # Dedup window: skip duplicate messages within N seconds (0 = off).
    LOG_DEDUP_WINDOW_SECONDS: int = 60

    # Max message length to embed. Longer messages get truncated.
    LOG_MAX_MESSAGE_LENGTH: int = 2000

    # Max log entries to store per service per hour (0 = unlimited).
    LOG_RATE_LIMIT_PER_SERVICE: int = 500

    # ── RAG / Similarity Thresholds ─────────────────────────────
    # Minimum cosine similarity (0–1) to include a result in query responses.
    # Lower = more results but noisier; higher = fewer but more precise.
    RAG_SIMILARITY_THRESHOLD: float = 0.3

    # Minimum similarity for auto-heal to diagnose an error log against code.
    # Below this the LLM would hallucinate root causes rather than find real ones.
    HEAL_MIN_SIMILARITY: float = 0.25

    # ── CORS ─────────────────────────────────────────────────────
    # Comma-separated list of allowed origins.
    # Defaults to the Next.js UI origin for local Docker Compose usage.
    # Set to "*" only for fully public, credential-free deployments.
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000,http://web:3000"

    # ── GitHub (Auto-Healing PR creation) ────────────────────────
    GITHUB_TOKEN: str = ""
    GITHUB_DEFAULT_REPO: str = ""

    # ── Auto-Ingest on startup (optional) ────────────────────────
    AUTO_INGEST_REPO_PATH: str = ""
    AUTO_INGEST_BRANCH: str = "HEAD"
    AUTO_INGEST_MAX_COMMITS: int = 100

    # ── Multi-Space Embeddings ───────────────────────────────────
    # Separate model for log/doc domain. Defaults to same as code model.
    # Set to e.g. "sentence-transformers/all-MiniLM-L6-v2" for a lighter
    # general-purpose model for log concepts and documentation.
    LOG_EMBEDDING_MODEL_NAME: str = ""  # empty = use EMBEDDING_MODEL_NAME

    # ── Embedding performance (auto-tuned from detected RAM) ────
    # Defaults are computed from system/container memory at import time.
    # Override any value via env var (e.g. EMBED_BATCH_SIZE=16).
    #
    # Detected RAM: see DETECTED_RAM_GB / _RAM_TIER_LABEL above.
    EMBED_BATCH_SIZE: int = _T["batch"]
    EMBED_WORKERS: int = _T["workers"]
    EMBED_ENCODE_BATCH_SIZE: int = _T["encode_bs"]
    EMBED_MAX_CHARS: int = _T["chars"]
    # Token-level hard cap passed to model.max_seq_length.
    # 0 = use the model's built-in limit (8192 for jina-v2).
    EMBED_MAX_SEQ_LENGTH: int = _T["seq"]

    # ── Feature gates (auto-disabled on low-memory systems) ──────
    # Automatically skipped when RAM ≤ 8 GB so the Ollama model and
    # dep-extraction memory don't compete with embedding.
    DISABLE_CODEBASE_SUMMARY: bool = _T["no_summary"]
    DISABLE_DEP_EXTRACTION: bool = _T["no_deps"]

    # ── Dependency graph source ──────────────────────────────────
    # "breadth" — scan with Breadth (tree-sitter, Terraform, Kubernetes)
    # "regex"   — legacy regex extractor (service-to-service only)
    # "auto"    — Breadth when it is installed, otherwise regex
    DEP_GRAPH_SOURCE: str = "auto"
    # Path to the Breadth CLI: a command on PATH or a breadth.mjs file.
    BREADTH_BIN: str = "breadth"
    BREADTH_SCAN_TIMEOUT_SECONDS: int = 300

    # ── Ollama override ──────────────────────────────────────────
    # On ≤8 GB the default backend is *not* Ollama (it's too slow on CPU).
    # If you still want local Ollama, set FORCE_OLLAMA=true — but expect
    # 3–5+ minutes per query on a 2-vCPU / 8 GB machine.
    FORCE_OLLAMA: bool = False

    @model_validator(mode="after")
    def _apply_force_ollama(self):
        """When FORCE_OLLAMA=true, override LLM_BACKEND to 'ollama'."""
        if self.FORCE_OLLAMA:
            self.LLM_BACKEND = "ollama"
        return self

    @property
    def LLM_AVAILABLE(self) -> bool:
        """Whether the configured LLM backend is actually usable.

        Rules:
          - ollama → always True (local, no API key needed)
          - openai  → True only if OPENAI_API_KEY is set
          - anthropic → True only if ANTHROPIC_API_KEY is set
          - bedrock → True (uses IAM, no key needed in env)
        """
        backend = self.LLM_BACKEND.lower()
        if backend == "ollama":
            return True
        if backend == "openai":
            return bool(self.OPENAI_API_KEY)
        if backend == "anthropic":
            return bool(self.ANTHROPIC_API_KEY)
        if backend == "bedrock":
            return True  # uses IAM credentials, not an env-var key
        if backend == "gemini":
            return bool(self.GEMINI_API_KEY)
        return False

    # ── Embedding versioning (blue/green index) ──────────────────
    # Bump this when you switch EMBEDDING_MODEL_NAME so new rows are tagged
    # and old rows can be background-migrated without corrupting queries.
    EMBEDDING_MODEL_VERSION: str = "v1"

    # ── Auto-Heal Trust Ladder ───────────────────────────────────
    # Minimum confidence (0–1) to auto-apply a fix to staging.
    HEAL_AUTO_APPLY_MIN_CONFIDENCE: float = 0.80
    # blast_radius threshold: services with > this many downstream deps = "high"
    HEAL_BLAST_RADIUS_HIGH_THRESHOLD: int = 5
    HEAL_BLAST_RADIUS_MEDIUM_THRESHOLD: int = 2

    # ── LogConcept Pipeline ──────────────────────────────────────
    # Drain3 similarity threshold for matching a log to an existing cluster.
    LOG_CONCEPT_DRAIN_SIM_THRESHOLD: float = 0.4
    # Maximum number of LogConcept clusters per service before eviction.
    LOG_CONCEPT_MAX_CLUSTERS: int = 2000
    # Hours of temporal histogram to retain per concept.
    LOG_CONCEPT_HISTOGRAM_HOURS: int = 24

    # ── Query Planner ────────────────────────────────────────────
    # Enable the query classifier and parallel retrieval planner.
    QUERY_PLANNER_ENABLED: bool = True
    # Max graph traversal depth for impact/diagnostic queries.
    QUERY_PLANNER_MAX_GRAPH_DEPTH: int = 3

    # ── Causation Evidence Thresholds ────────────────────────────
    # p-value threshold for Granger causality to promote correlates_with → probable_cause.
    CAUSATION_GRANGER_P_THRESHOLD: float = 0.05
    # Minimum number of incidents before promoting to probable_cause.
    CAUSATION_MIN_INCIDENTS: int = 3


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (reads env once)."""
    return Settings()
