"""
RootOps — Service Dependency Extractor

Statically analyses code files to discover cross-service dependencies.
Runs after ingestion on every file in the repository tree and writes
ServiceDependency rows for each detected call site.

Detected dependency types:
  http    — requests/httpx/fetch/axios calls to another service URL
  event   — Kafka, Pub/Sub, SQS, RabbitMQ publish/send calls
  grpc    — grpc channel/stub construction referencing another service
  import  — direct imports of another known service's shared library
  env_ref — reads of env vars that resolve to another service (e.g. PAYMENT_SERVICE_URL)

Design decisions:
  - All detection is pure regex — zero LLM calls, so it's fast and deterministic.
  - Known service names are resolved from the repositories table so newly
    ingested repos are automatically linked.
  - Each unique (source_repo, target_name, dep_type, source_file) tuple is
    upserted (not duplicated) via the unique index on the table.
  - Confidence is always 1.0 for regex matches; an LLM extraction path
    can set lower confidence in future.
"""

from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Pattern library ───────────────────────────────────────────────
# Each entry: (dep_type, compiled_regex)
# The regex must have a named group `target` that captures the service
# name or a URL segment from which the name can be extracted.

_PATTERNS: list[tuple[str, re.Pattern]] = [
    # HTTP: requests / httpx / urllib
    ("http", re.compile(
        r"""(?:requests|httpx|aiohttp|urllib)\s*\.\s*(?:get|post|put|patch|delete|request)\s*\(\s*['"]https?://([a-zA-Z0-9_.-]+)""",
        re.IGNORECASE,
    )),
    # HTTP: fetch / axios / got (JS/TS)
    ("http", re.compile(
        r"""(?:fetch|axios(?:\s*\.\s*(?:get|post|put|delete|patch))?|got)\s*\(\s*`?['"]https?://([a-zA-Z0-9_.-]+)""",
        re.IGNORECASE,
    )),
    # HTTP: f-string / template with env var URL (Python)
    ("http", re.compile(
        r"""f?['"]https?://\{?(?:os\.(?:environ\.get|getenv)\s*\(\s*)?['"]?([A-Z][A-Z0-9_]*(?:_URL|_HOST|_ENDPOINT|_SERVICE))['")?}]""",
        re.IGNORECASE,
    )),
    # gRPC: grpc.insecure_channel / grpc.secure_channel
    ("grpc", re.compile(
        r"""grpc\s*\.\s*(?:insecure_channel|secure_channel)\s*\(\s*f?['"]([a-zA-Z0-9_.-]+)""",
        re.IGNORECASE,
    )),
    # gRPC (Go): grpc.Dial
    ("grpc", re.compile(
        r"""grpc\.Dial\s*\(\s*"([a-zA-Z0-9_.-]+)""",
    )),
    # Kafka / Confluent producer
    ("event", re.compile(
        r"""(?:KafkaProducer|Producer)\s*\(.*?bootstrap[._-]servers\s*[=:]\s*['"]([a-zA-Z0-9_.-]+)""",
        re.IGNORECASE | re.DOTALL,
    )),
    # producer.send / producer.produce (topic as identifier signal)
    ("event", re.compile(
        r"""(?:producer|client)\s*\.\s*(?:send|produce|publish)\s*\(\s*['"]([a-zA-Z0-9._-]+)['"]""",
        re.IGNORECASE,
    )),
    # Google Pub/Sub publish
    ("event", re.compile(
        r"""PublisherClient\s*\(\s*\).*?\.publish\s*\(\s*['"]projects/[^'"]+/topics/([a-zA-Z0-9_.-]+)['"]""",
        re.IGNORECASE | re.DOTALL,
    )),
    # AWS SQS
    ("event", re.compile(
        r"""\.send_message\s*\(\s*QueueUrl\s*=.*?/([a-zA-Z0-9_.-]+)['"]""",
        re.IGNORECASE | re.DOTALL,
    )),
    # Python direct import
    ("import", re.compile(
        r"""^(?:from|import)\s+([\w.]+)""",
        re.MULTILINE,
    )),
    # Go import
    ("import", re.compile(
        r"""\"(github\.com/[^\"]+/[^\"]+)\"""",
    )),
    # Env var references resolving to service URLs
    ("env_ref", re.compile(
        r"""os\.(?:environ\.get|getenv)\s*\(\s*['"]([A-Z][A-Z0-9_]*(?:_URL|_HOST|_ENDPOINT|_SERVICE|_ADDR))['"]""",
        re.IGNORECASE,
    )),
    # process.env.SERVICE_URL (JS/TS)
    ("env_ref", re.compile(
        r"""process\.env\.([A-Z][A-Z0-9_]*(?:_URL|_HOST|_ENDPOINT|_SERVICE|_ADDR))""",
    )),
]

# ── Service name normalisation ────────────────────────────────────

def _normalise(raw: str) -> str | None:
    """
    Extract a plausible service name from a matched string.

    Rules:
    - Strip URL schemes, ports, localhost, common TLDs.
    - Strip suffixes like _URL, _HOST, _ENDPOINT, _SERVICE, _ADDR from env vars.
    - Convert underscores/dots to hyphens for consistency.
    - Return None if the result is too generic (e.g. 'localhost', 'example').
    """
    s = raw.strip().lower()

    # Remove URL scheme
    s = re.sub(r"^https?://", "", s)

    # Remove port
    s = re.sub(r":\d+$", "", s)

    # Strip known suffixes from env-var style names
    s = re.sub(r"[_-](?:url|host|endpoint|service|addr)$", "", s, flags=re.IGNORECASE)

    # Normalise separators
    s = s.replace("_", "-").replace(".", "-")

    # Reject generic / local names
    _IGNORE = {
        "localhost", "127-0-0-1", "0-0-0-0", "example", "example-com",
        "your-service", "service", "host", "api", "internal", "",
    }
    if s in _IGNORE or len(s) < 3:
        return None

    return s


# ── Import-specific filtering ─────────────────────────────────────

def _is_known_service(name: str, known_names: set[str]) -> bool:
    """
    For import-type matches, only accept if the module name
    contains a known service name as a component.
    """
    parts = re.split(r"[./]", name.lower())
    return any(p in known_names for p in parts)


# ── Main extraction function ──────────────────────────────────────

def extract_dependencies(
    source_content: str,
    source_file: str,
    known_service_names: set[str],
) -> list[dict]:
    """
    Scan a single file's content for cross-service dependency patterns.

    Args:
        source_content:      Full text of the source file.
        source_file:         Repo-relative file path (for evidence).
        known_service_names: Set of lowercase service names currently in the
                             repositories table (used to filter import matches).

    Returns:
        List of dependency dicts, each with keys:
          target_name, dependency_type, source_file, source_pattern
        Caller is responsible for deduplication and DB write.
    """
    found: list[dict] = []

    for dep_type, pattern in _PATTERNS:
        for match in pattern.finditer(source_content):
            raw = match.group(1)
            target_name = _normalise(raw)
            if not target_name:
                continue

            # For import matches, only record if target is a known service
            if dep_type == "import" and not _is_known_service(raw, known_service_names):
                continue

            found.append({
                "target_name": target_name,
                "dependency_type": dep_type,
                "source_file": source_file,
                "source_pattern": match.group(0)[:200],  # cap at 200 chars
            })

    return found


# ── DB writer ─────────────────────────────────────────────────────

async def extract_and_persist_dependencies(
    session: AsyncSession,
    source_repo_id: uuid.UUID,
    source_repo_name: str,
    file_contents: list[tuple[str, str]],  # [(file_path, content), ...]
) -> int:
    """
    Run extraction on all files and upsert ServiceDependency rows.

    Args:
        session:          Async DB session.
        source_repo_id:   UUID of the source repository.
        source_repo_name: Display name of the source repo.
        file_contents:    List of (relative_file_path, content) tuples.

    Returns:
        Number of dependency rows upserted.
    """
    from app.models.repository import Repository
    from app.models.service_dependency import ServiceDependency

    # Load all known service names for import filtering
    result = await session.execute(select(Repository.name, Repository.id))
    repo_rows = result.fetchall()
    known_names: set[str] = {r.name.lower() for r in repo_rows}
    name_to_id: dict[str, uuid.UUID] = {r.name.lower(): r.id for r in repo_rows}

    upsert_count = 0

    for file_path, content in file_contents:
        deps = extract_dependencies(content, file_path, known_names)
        for dep in deps:
            target_name = dep["target_name"]
            # Don't create a self-dependency
            if target_name == source_repo_name.lower():
                continue

            target_id = name_to_id.get(target_name)

            stmt = (
                pg_insert(ServiceDependency)
                .values(
                    id=uuid.uuid4(),
                    source_repo_id=source_repo_id,
                    source_repo_name=source_repo_name,
                    target_repo_name=target_name,
                    target_repo_id=target_id,
                    dependency_type=dep["dependency_type"],
                    source_file=dep["source_file"],
                    source_pattern=dep["source_pattern"],
                    call_count=1,
                    confidence=1.0,
                )
                .on_conflict_do_update(
                    constraint="uq_dep_source_target_type_file",
                    set_={"call_count": ServiceDependency.call_count + 1},
                )
            )
            await session.execute(stmt)
            upsert_count += 1

    await session.flush()
    logger.info(
        "Dependency extraction: %d patterns found across %d files for %s",
        upsert_count,
        len(file_contents),
        source_repo_name,
    )
    return upsert_count
