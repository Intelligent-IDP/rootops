# ─── RootOps API — Multi-stage build ──────────────────────────────
#
# Stage 1 (builder): compiles C extensions (asyncpg, cryptography)
#                    which need gcc and libpq-dev.
# Stage 2 (breadth): builds the Breadth CLI used for the dependency graph.
# Stage 3 (runtime): copies only the installed packages — no build
#                    tools, no compiler, no headers in the final image.
#
# This saves ~150 MB compared to a single-stage build.

# ── Stage 1: build ────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

# Build-time system deps: gcc for asyncpg/cryptography, libpq-dev for
# psycopg2/asyncpg headers. These stay in the builder, not the final image.
RUN echo 'Acquire::Retries "10";' > /etc/apt/apt.conf.d/80-retries && \
    apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv and sync dependencies.
# Copy lockfiles first to maximise Docker layer caching —
# this layer is only invalidated when dependencies actually change.
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-cache --no-install-project && \
    cp -r .venv/lib/python3.11/site-packages/* /usr/local/lib/python3.11/site-packages/ && \
    cp .venv/bin/uvicorn /usr/local/bin/uvicorn 2>/dev/null || true

# ── Stage 2: Breadth (dependency graph) ───────────────────────────
# Breadth maps code, Terraform and Kubernetes into one graph with
# tree-sitter. RootOps runs `breadth scan` during ingestion to build the
# service dependency graph. Only the bundled CLI and its two tree-sitter
# packages are kept.
FROM node:20-bookworm-slim AS breadth

ARG BREADTH_REPO=https://github.com/theorjiugovictor/breadth
ARG BREADTH_REF=main

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN git clone --depth 1 --branch "$BREADTH_REF" "$BREADTH_REPO" . && \
    npm ci --no-audit --no-fund && \
    npm run build -w cli

WORKDIR /opt/breadth
RUN cp /src/cli/dist/breadth.mjs . && \
    echo '{"type":"module","private":true}' > package.json && \
    node -e "const d=require('/src/cli/package.json').dependencies; console.log(['web-tree-sitter','tree-sitter-wasms'].map(n => n + '@' + d[n]).join(' '))" \
      | xargs npm install --no-audit --no-fund --omit=dev && \
    node breadth.mjs --help > /dev/null

# ── Stage 3: runtime ──────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

# Runtime-only system deps:
#   libpq5  — asyncpg shared library (no headers needed at runtime)
#   git     — gitpython clones repos at runtime
#   curl    — healthcheck probe
#   libstdc++6 — Node.js runtime for Breadth
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 git curl libstdc++6 && \
    rm -rf /var/lib/apt/lists/*

# Breadth CLI (see stage 2) and the Node.js binary it runs on.
COPY --from=breadth /usr/local/bin/node /usr/local/bin/node
COPY --from=breadth /opt/breadth /opt/breadth
ENV BREADTH_BIN=/opt/breadth/breadth.mjs

WORKDIR /app

# Copy compiled Python packages from builder's venv (no gcc/headers included).
COPY --from=builder /app/.venv/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy application source (respects .dockerignore — excludes .git,
# tests/, scripts/, __pycache__, etc.)
COPY . .

# Non-root user with writable cache dir for HuggingFace models
RUN groupadd -r rootops && useradd -r -g rootops -m rootops \
    && mkdir -p /home/rootops/.cache \
    && chown -R rootops:rootops /app /home/rootops
USER rootops

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl --fail http://localhost:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
