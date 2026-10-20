# RootOps Architecture

RootOps is a self-hostable developer intelligence application. The web UI, API,
and PostgreSQL database run as separate Docker Compose services. The API owns
repository and log ingestion, retrieval, graph operations, and LLM dispatch.

## Components

- **Next.js web app (`web/`)** provides the dashboard and calls the FastAPI API.
- **FastAPI (`api/app/`)** exposes ingestion, query, log, graph, profile, PR
  review, and auto-heal endpoints.
- **Service layer (`api/app/services/`)** implements Git ingestion, chunking,
  embeddings, retrieval and reranking, log processing, graph construction,
  causation analysis, and LLM provider clients.
- **PostgreSQL with pgvector** stores repositories, commits, code chunks, logs,
  profiles, graph entities, and graph edges. Code and log embeddings use
  pgvector `HALFVEC` columns (16-bit floating-point values).
- **Ollama** is the default local LLM in Docker Compose. OpenAI, Anthropic,
  Gemini, and AWS Bedrock can be selected explicitly.

## Main Data Flows

### Repository ingestion

1. The API receives a local repository path or Git URL.
2. The Git ingestor reads files and commit history, chunks source, and creates
   embeddings locally.
3. Chunks, commits, and repository metadata are stored in PostgreSQL.
4. Breadth scans code and infrastructure configuration to produce structural
   service and dependency edges. When enabled and a supported LLM is available,
   `scan --llm` also summarizes undocumented, highly connected nodes. The legacy
   regex extractor is available as a fallback.

RootOps stores Breadth's full flowgraph in `breadth_graph_snapshots` for the
hierarchical Dependency Graph UI, while separately rolling service-level
structural edges into the knowledge graph for causation and blast-radius queries.

The Cloud Run configuration explicitly enables dependency extraction and selects
Breadth, overriding the low-memory default that otherwise skips graph creation.
Existing repositories must be re-ingested after enabling it to create a snapshot.
Graph nodes can be dragged within the current view; Reset node layout restores
the automatic layout. Positions are not persisted across reloads or filters.

### Query

1. The API embeds the question and retrieves relevant code and log records from
   pgvector, optionally using query planning and reranking.
2. With the default Ollama configuration, retrieved context is synthesized by
   the local model. When a hosted provider is configured, retrieved context is
   sent to that provider. Raw vector search is also available without LLM
   synthesis.
3. The API returns an answer with source chunks, log matches, and retrieval
   metadata.

### Logs and causation

Logs can be submitted through the API or the OTLP/HTTP receiver. They are
filtered, deduplicated, embedded, and stored alongside code. Structural graph
edges describe how services are wired; causal edges represent evidence-based
relationships and are maintained separately.

The homepage System Health section polls `GET /api/ingest/logs/health` every
30 seconds. It summarizes up to 500 retained events from the last hour and
exposes up to 20 recent evidence records. Severity filters, deduplication, and
rate limits apply before storage, so these counts are not request error rates
or availability measurements. An incomplete sample is explicitly flagged.

Events with `synthetic=true` metadata or reserved `mock-` service names are
separated from live health assessment. Shared `request_id` values group related
events, but do not establish causation, dependency direction, or a full trace.
No errors, no telemetry, and synthetic-only telemetry are reported as
insufficient evidence, never as proof of a healthy system. This view does not
overlay runtime calls onto the static Breadth map; that requires span ingestion
and trace-to-graph mapping.

Live service evidence is shown first. Synthetic services, correlation groups,
and their analysis are kept inside a **Demo evidence** section collapsed by default.

Each live service has an **Analyze live evidence** action using
`POST /api/ingest/logs/live-analysis`. It accepts 1–20 event UUIDs from a single
live service, an optional `repo_id`, and optional `use_llm` (default `true`).
Synthetic, missing, and mixed-service evidence is rejected. Only the selected
log messages and up to three code excerpts from the explicitly selected
repository are passed to the configured LLM; personal log metadata and global
log retrieval are not included. Code references identify possible paths, not
proof of execution. Provider or source-lookup failures leave an explicitly
labelled evidence-only or logs-only result. This does not change health status,
confirm causation, or apply fixes.

Synthetic correlation groups expose an **Analyze scenario** action through
`POST /api/ingest/logs/mock-analysis`, accepting 2–20 event UUIDs and optional
`use_llm` (default `true`). The API requires every selected record to be
synthetic and to share a nonempty request ID. Only those selected log records
are sent to the configured LLM; no global retrieval or production evidence is
included. Responses contain a chronological evidence timeline and either an
LLM interpretation or an explicitly labelled evidence-only fallback if the
provider is unavailable, fails, or exceeds 30 seconds. This analysis does not
promote causal edges, apply fixes, or change live system health.

## Data and Privacy Boundaries

The default Compose configuration runs the database, embeddings, and Ollama
locally. Selecting a hosted LLM sends the context needed for that request to
the selected provider. The optional GitHub integration communicates with
GitHub for repository operations and pull-request creation. Model weights may
be downloaded from their configured registries during setup.

Breadth LLM summaries are enabled by default when the selected RootOps provider
is available. They send source excerpts for up to 50 code nodes to that provider;
set `BREADTH_LLM_ENABLED=false` to disable them. Breadth's built-in providers
support Ollama, OpenAI, Anthropic, Gemini, and OpenAI-compatible endpoints;
AWS Bedrock is not currently wired to Breadth's LLM provider.

The API does not currently provide user authentication. Caddy in the optional
cloud deployment provides HTTPS, not access control. Keep deployments on a
trusted private network or add an external authentication and network boundary
before exposing them publicly.

## Deployment and Operations

- **Local or private single machine:** `make up` creates the default `.env`,
  pulls the application images, starts Docker Compose, and downloads the
  configured Ollama model.
- **GCP VM:** `deploy/cloud/deploy-gcp.sh` provisions a VM and configures
   Caddy/HTTPS with Duck DNS. Ingress is restricted to the deploying machine's
   detected public IPv4 by default; set `GCP_ALLOWED_CIDR` for a trusted VPN or
   office network. The application itself does not provide user authentication.
- **Other platforms:** operators must provide persistent PostgreSQL with
  pgvector, secure networking, TLS, and authentication.

The current stack performs ingestion in the API service; there is no separate
job queue or worker tier. PostgreSQL is the persistent source of truth.

## Vector Storage

Embeddings are currently stored as 16-bit `HALFVEC` values and queried through
pgvector. TurboQuant or another lower-bit quantizer should be considered only
after measuring storage or retrieval as a bottleneck on representative data,
and validating recall and query latency against the current half-precision
baseline. It is not enabled in the current architecture.
