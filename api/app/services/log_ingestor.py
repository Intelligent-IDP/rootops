"""
RootOps — Log Ingestor Service

Parses raw log text (plain text or JSON), extracts structured fields
(timestamp, level, file references, stack traces), **filters** by
severity / service / dedup / rate-limit, embeds each surviving entry,
and stores it in pgvector for cross-correlation with code chunks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from threading import Lock

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.log_entry import LogEntry
from app.services.embedding import embed_batch

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Severity ranking (higher = more severe) ──────────────────────
_SEVERITY_RANK: dict[str, int] = {
    "TRACE": 0,
    "DEBUG": 1,
    "INFO": 2,
    "WARN": 3,
    "WARNING": 3,
    "ERROR": 4,
    "FATAL": 5,
    "CRITICAL": 5,
}

# ── Dedup sliding window ────────────────────────────────────────
_dedup_lock = Lock()
_dedup_cache: dict[str, float] = {}  # hash → timestamp (epoch)
_DEDUP_CACHE_MAX = 10_000  # evict oldest after this many entries

# ── Rate-limit counters (per service, per hour) ─────────────────
_rate_lock = Lock()
_rate_counters: dict[str, dict[str, int]] = {}  # {service: {hour_key: count}}


def _severity_rank(level: str | None) -> int:
    """Return the numeric rank for a severity string."""
    if not level:
        return -1
    return _SEVERITY_RANK.get(level.upper(), -1)


def _parse_allowed_services() -> set[str] | None:
    """Parse LOG_ALLOWED_SERVICES into a set, or None if empty (accept all)."""
    raw = settings.LOG_ALLOWED_SERVICES.strip()
    if not raw:
        return None
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _dedup_key(message: str, service: str) -> str:
    """Hash a message + service for dedup lookup."""
    return hashlib.sha256(f"{service}:{message}".encode()).hexdigest()[:16]


def _is_duplicate(message: str, service: str, now: float) -> bool:
    """Check if we've seen this exact message recently (within dedup window)."""
    window = settings.LOG_DEDUP_WINDOW_SECONDS
    if window <= 0:
        return False

    key = _dedup_key(message, service)

    with _dedup_lock:
        last_seen = _dedup_cache.get(key)
        if last_seen and (now - last_seen) < window:
            return True

        # Record this message
        _dedup_cache[key] = now

        # Evict oldest entries if cache is too large
        if len(_dedup_cache) > _DEDUP_CACHE_MAX:
            sorted_keys = sorted(_dedup_cache, key=_dedup_cache.get)  # type: ignore[arg-type]
            for old_key in sorted_keys[: len(_dedup_cache) - _DEDUP_CACHE_MAX]:
                _dedup_cache.pop(old_key, None)

    return False


def _check_rate_limit(service: str) -> bool:
    """Return True if this service has exceeded its hourly rate limit."""
    limit = settings.LOG_RATE_LIMIT_PER_SERVICE
    if limit <= 0:
        return False

    hour_key = datetime.now(tz=UTC).strftime("%Y-%m-%d-%H")

    with _rate_lock:
        svc_counters = _rate_counters.setdefault(service, {})
        current = svc_counters.get(hour_key, 0)

        if current >= limit:
            return True

        svc_counters[hour_key] = current + 1

        # Clean up old hour keys
        old_keys = [k for k in svc_counters if k != hour_key]
        for k in old_keys:
            del svc_counters[k]

    return False


def filter_log_entries(parsed: list[dict]) -> tuple[list[dict], dict]:
    """Apply all configured filters to parsed log entries.

    Returns:
        (kept, drop_stats) — the surviving entries and a breakdown of why
        entries were dropped.
    """
    min_severity = settings.LOG_MIN_SEVERITY.upper()
    min_rank = _SEVERITY_RANK.get(min_severity, 3)  # default WARN
    allowed_services = _parse_allowed_services()
    max_msg_len = settings.LOG_MAX_MESSAGE_LENGTH
    now = datetime.now(tz=UTC).timestamp()

    kept: list[dict] = []
    drop_stats: dict[str, int] = defaultdict(int)

    for entry in parsed:
        level = (entry.get("level") or "").upper()
        service = (entry.get("service_name") or "").lower()
        message = entry.get("message") or ""

        # ── 1. Severity gate ─────────────────────────────────────
        entry_rank = _severity_rank(level)
        if entry_rank < min_rank and entry_rank >= 0:
            drop_stats["below_severity"] += 1
            continue

        # ── 2. Service allowlist ─────────────────────────────────
        if allowed_services and service not in allowed_services:
            drop_stats["service_not_allowed"] += 1
            continue

        # ── 3. Dedup ─────────────────────────────────────────────
        if _is_duplicate(message, service, now):
            drop_stats["duplicate"] += 1
            continue

        # ── 4. Rate limit ────────────────────────────────────────
        if _check_rate_limit(service):
            drop_stats["rate_limited"] += 1
            continue

        # ── 5. Truncate long messages ────────────────────────────
        if len(message) > max_msg_len:
            entry["message"] = message[:max_msg_len] + "… [truncated]"
            drop_stats["truncated"] += 1  # not dropped, just trimmed

        kept.append(entry)

    total_dropped = sum(
        v for k, v in drop_stats.items() if k != "truncated"
    )
    if total_dropped:
        logger.info(
            "Filtered %d / %d log entries: %s",
            total_dropped,
            len(parsed),
            dict(drop_stats),
        )

    return kept, dict(drop_stats)

# ── Log line parsing patterns ────────────────────────────────────

# Common timestamp formats
_TS_PATTERNS = [
    # ISO 8601: 2024-03-01T12:34:56Z or 2024-03-01T12:34:56.123+00:00
    re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
    # Simple: 2024-03-01 12:34:56
    re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"),
]

# Log levels
_LEVEL_PATTERN = re.compile(
    r"\b(CRITICAL|FATAL|ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE)\b",
    re.IGNORECASE,
)

# File + line references in stack traces
# Matches: File "payment.py", line 47 OR payment.py:47 OR at com.foo.Bar(Bar.java:123)
_FILE_REF_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+)'),        # Python
    re.compile(r"at\s+\S+\((\S+\.java):(\d+)\)"),     # Java
    re.compile(r"(\S+\.\w{1,4}):(\d+)"),              # Generic file:line
]

# Exception class names
_EXCEPTION_PATTERN = re.compile(
    r"\b(\w*(?:Error|Exception|Fault|Failure|Panic)\b[^\n]*)",
    re.IGNORECASE,
)


def _parse_timestamp(text: str) -> datetime | None:
    """Try to extract a timestamp from a log line."""
    for pattern in _TS_PATTERNS:
        match = pattern.search(text)
        if match:
            ts_str = match.group(1)
            for fmt in [
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S",
            ]:
                try:
                    return datetime.strptime(ts_str, fmt).replace(
                        tzinfo=UTC
                    )
                except ValueError:
                    continue
    return None


def _parse_level(text: str) -> str | None:
    """Extract log level from a line."""
    match = _LEVEL_PATTERN.search(text)
    if match:
        level = match.group(1).upper()
        if level == "WARNING":
            level = "WARN"
        return level
    return None


def _parse_file_reference(text: str) -> tuple[str | None, int | None]:
    """Extract file path and line number from stack traces."""
    for pattern in _FILE_REF_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1), int(match.group(2))
    return None, None


def _parse_error(text: str) -> str | None:
    """Extract exception/error class and message."""
    match = _EXCEPTION_PATTERN.search(text)
    return match.group(1).strip() if match else None


def parse_log_lines(
    raw_text: str,
    service_name: str,
    source: str = "raw",
) -> list[dict]:
    """Parse raw log text into structured log entry dicts.

    Supports:
    - Plain text logs (one entry per line or multi-line with stack traces)
    - JSON-formatted logs (one JSON object per line)

    Returns:
        List of dicts ready for LogEntry creation.
    """
    entries: list[dict] = []
    lines = raw_text.strip().splitlines()

    if not lines:
        return entries

    # ── Detect JSON logs ─────────────────────────────────────────
    first_line = lines[0].strip()
    is_json = first_line.startswith("{")

    if is_json:
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                message_text = data.get("message", data.get("msg", line))
                parsed_level = (
                    data.get("level", data.get("severity", ""))
                    or ""
                ).upper() or None
                if not parsed_level:
                    parsed_level = _parse_level(message_text)

                file_ref = data.get("file", None)
                line_ref = data.get("line", None)
                if not file_ref and not line_ref:
                    file_ref, line_ref = _parse_file_reference(message_text)

                entries.append({
                    "service_name": service_name,
                    "source": source,
                    "timestamp": _parse_timestamp(
                        data.get("timestamp", data.get("time", ""))
                    ),
                    "level": parsed_level,
                    "message": message_text,
                    "parsed_error": data.get("error", data.get("exception", None)) or _parse_error(message_text),
                    "file_reference": file_ref,
                    "line_reference": line_ref,
                    "metadata_json": {
                        k: v for k, v in data.items()
                        if k not in {
                            "timestamp", "time", "level", "severity",
                            "message", "msg", "error", "exception",
                            "file", "line",
                        }
                    } or None,
                })
            except json.JSONDecodeError:
                # Fall back to plain text parsing
                entries.append(_parse_plain_line(line, service_name, source))
    else:
        # ── Plain text logs ──────────────────────────────────────
        # Group multi-line stack traces with their parent log line
        current_block: list[str] = []
        for line in lines:
            # If line starts with a timestamp or level, it's a new entry
            has_ts = any(p.search(line) for p in _TS_PATTERNS)
            has_level = _LEVEL_PATTERN.search(line) is not None

            if (has_ts or has_level) and current_block:
                entries.append(
                    _parse_plain_line(
                        "\n".join(current_block), service_name, source
                    )
                )
                current_block = []

            current_block.append(line)

        # Don't forget the last block
        if current_block:
            entries.append(
                _parse_plain_line(
                    "\n".join(current_block), service_name, source
                )
            )

    logger.info("Parsed %d log entries from %s (%s)", len(entries), service_name, source)
    return entries


def _parse_plain_line(text: str, service_name: str, source: str) -> dict:
    """Parse a single plain-text log entry (possibly multi-line)."""
    file_ref, line_ref = _parse_file_reference(text)
    return {
        "service_name": service_name,
        "source": source,
        "timestamp": _parse_timestamp(text),
        "level": _parse_level(text),
        "message": text,
        "parsed_error": _parse_error(text),
        "file_reference": file_ref,
        "line_reference": line_ref,
        "metadata_json": None,
    }


async def ingest_logs(
    raw_text: str,
    service_name: str,
    session: AsyncSession,
    *,
    source: str = "raw",
) -> dict:
    """Parse, embed, and store log entries.

    Returns:
        Summary dict with ingestion stats.
    """
    # ── 1. Parse ─────────────────────────────────────────────────
    parsed = parse_log_lines(raw_text, service_name, source)
    if not parsed:
        return {"entries_ingested": 0, "by_level": {}, "dropped": 0, "drop_reasons": {}}

    # ── 2. Filter (severity, service, dedup, rate-limit) ────────
    kept, drop_stats = filter_log_entries(parsed)
    if not kept:
        return {
            "entries_ingested": 0,
            "by_level": {},
            "dropped": len(parsed),
            "drop_reasons": drop_stats,
            "service_name": service_name,
        }

    # ── 3. Embed surviving messages ──────────────────────────────
    messages = [entry["message"] for entry in kept]
    embeddings = await embed_batch(messages)

    # ── 4. Store in database ─────────────────────────────────────
    level_counts: dict[str, int] = {}
    for entry_data, embedding in zip(kept, embeddings):
        log_entry = LogEntry(
            id=uuid.uuid4(),
            service_name=entry_data["service_name"],
            source=entry_data["source"],
            timestamp=entry_data["timestamp"],
            level=entry_data["level"],
            message=entry_data["message"],
            parsed_error=entry_data["parsed_error"],
            file_reference=entry_data["file_reference"],
            line_reference=entry_data["line_reference"],
            embedding=embedding,
            metadata_json=entry_data["metadata_json"],
        )
        session.add(log_entry)

        level = entry_data["level"] or "UNKNOWN"
        level_counts[level] = level_counts.get(level, 0) + 1

    await session.flush()

    dropped = len(parsed) - len(kept)
    stats: dict = {
        "entries_ingested": len(kept),
        "by_level": level_counts,
        "service_name": service_name,
        "dropped": dropped,
        "drop_reasons": drop_stats,
    }

    # ── 5. Route through LogConcept pipeline (async, non-blocking) ──
    # Process raw log entries into aggregated LogConcepts. This runs
    # after the raw entries are stored so ingest latency is not affected
    # by the Drain3 / LLM Tier-3 clustering work.
    try:
        from app.services.log_concept_service import process_log_batch
        concept_stats = await process_log_batch(session, kept, service_name)
        stats["concept_stats"] = concept_stats
    except Exception as exc:
        logger.warning(
            "LogConcept pipeline failed for %s — raw entries still stored: %s",
            service_name, exc,
        )

    logger.info(
        "Ingested %d log entries for %s (dropped %d)",
        len(kept), service_name, dropped,
    )
    return stats
