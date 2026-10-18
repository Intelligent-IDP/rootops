"""
OpenTelemetry (OTLP) Log Receiver.

Accepts OTLP/HTTP log exports (JSON) and routes them through the existing
log parser/embedding pipeline.  This replaces the previous CloudWatch
integration with a vendor-neutral, standards-based approach.

Any OpenTelemetry SDK or Collector can export logs here:
    export endpoint = http://<rootops-api>:8000/v1/logs

Supported:
  • OTLP/HTTP JSON (application/json)
  • OTLP/HTTP Protobuf (application/x-protobuf) — decoded via
    opentelemetry-proto if installed, otherwise rejected gracefully.

See: https://opentelemetry.io/docs/specs/otlp/#otlphttp-request
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.services.log_ingestor import ingest_logs

logger = logging.getLogger(__name__)
settings = get_settings()

# ── In-memory stats for the OTEL receiver ────────────────────────
_otel_stats_lock = Lock()
_otel_stats: dict[str, Any] = {
    "enabled": False,
    "total_requests_received": 0,
    "total_log_records_received": 0,
    "total_entries_ingested": 0,
    "total_dropped": 0,
    "last_received_at": None,
    "services_seen": {},
}


def reset_otel_stats() -> None:
    """Reset receiver statistics (useful for tests)."""
    with _otel_stats_lock:
        _otel_stats.update(
            total_requests_received=0,
            total_log_records_received=0,
            total_entries_ingested=0,
            last_received_at=None,
            services_seen={},
        )


def get_otel_receiver_stats() -> dict:
    """Return a snapshot of OTEL receiver statistics."""
    with _otel_stats_lock:
        return {
            "enabled": settings.OTEL_LOGS_RECEIVER_ENABLED,
            **{k: v for k, v in _otel_stats.items() if k != "enabled"},
        }


# ── OTLP severity mapping ───────────────────────────────────────
# https://opentelemetry.io/docs/specs/otel/logs/data-model/#severity-fields
_SEVERITY_NUMBER_TO_LEVEL: dict[int, str] = {
    1: "TRACE", 2: "TRACE", 3: "TRACE", 4: "TRACE",
    5: "DEBUG", 6: "DEBUG", 7: "DEBUG", 8: "DEBUG",
    9: "INFO", 10: "INFO", 11: "INFO", 12: "INFO",
    13: "WARN", 14: "WARN", 15: "WARN", 16: "WARN",
    17: "ERROR", 18: "ERROR", 19: "ERROR", 20: "ERROR",
    21: "FATAL", 22: "FATAL", 23: "FATAL", 24: "FATAL",
}


def _nano_to_datetime(time_unix_nano: int | str | None) -> datetime | None:
    """Convert OTLP nanosecond timestamp to datetime."""
    if not time_unix_nano:
        return None
    try:
        ns = int(time_unix_nano)
        return datetime.fromtimestamp(ns / 1e9, tz=UTC)
    except (ValueError, OSError):
        return None


def _extract_severity(record: dict) -> str | None:
    """Extract log level from OTLP severity fields."""
    # Prefer severityText if present
    severity_text = record.get("severityText", "")
    if severity_text:
        level = severity_text.upper()
        if level == "WARNING":
            level = "WARN"
        return level

    # Fall back to severityNumber
    severity_number = record.get("severityNumber")
    if severity_number and isinstance(severity_number, int):
        return _SEVERITY_NUMBER_TO_LEVEL.get(severity_number)

    return None


def _extract_body(record: dict) -> str:
    """Extract the log message body from an OTLP LogRecord."""
    body = record.get("body")
    if body is None:
        return ""

    # OTLP body is an AnyValue — can be stringValue, kvlistValue, etc.
    if isinstance(body, dict):
        if "stringValue" in body:
            return body["stringValue"]
        if "kvlistValue" in body:
            # Convert key-value list to readable string
            pairs = body["kvlistValue"].get("values", [])
            return " ".join(
                f"{p.get('key', '')}={_extract_any_value(p.get('value', {}))}"
                for p in pairs
            )
        # Fallback: serialise the whole body
        return json.dumps(body)

    return str(body)


def _extract_any_value(val: dict) -> str:
    """Convert an OTLP AnyValue to a plain string."""
    if not val or not isinstance(val, dict):
        return str(val)
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in val:
            return str(val[key])
    if "arrayValue" in val:
        items = val["arrayValue"].get("values", [])
        return "[" + ", ".join(_extract_any_value(v) for v in items) + "]"
    if "kvlistValue" in val:
        pairs = val["kvlistValue"].get("values", [])
        return "{" + ", ".join(
            f"{p.get('key', '')}={_extract_any_value(p.get('value', {}))}"
            for p in pairs
        ) + "}"
    return json.dumps(val)


def _attributes_to_dict(attributes: list[dict] | None) -> dict[str, str]:
    """Convert OTLP KeyValue[] attributes to a flat dict."""
    if not attributes:
        return {}
    result: dict[str, str] = {}
    for kv in attributes:
        key = kv.get("key", "")
        value = kv.get("value", {})
        result[key] = _extract_any_value(value)
    return result


def _hex_or_none(val: str | None) -> str | None:
    """Return a hex trace/span ID or None if empty/zero."""
    if not val:
        return None
    # OTLP sends base64 or hex; JSON encoding is typically base64
    stripped = val.strip()
    if not stripped or stripped == "AAAAAAAAAAAAAAAAAAAAAA==" or all(c == "0" for c in stripped):
        return None
    return stripped


def parse_otlp_logs_json(payload: dict) -> list[dict]:
    """Parse an OTLP ExportLogsServiceRequest (JSON) into internal format.

    The OTLP JSON structure:
    {
      "resourceLogs": [{
        "resource": { "attributes": [...] },
        "scopeLogs": [{
          "scope": { "name": "..." },
          "logRecords": [{
            "timeUnixNano": "...",
            "severityNumber": 17,
            "severityText": "ERROR",
            "body": { "stringValue": "..." },
            "attributes": [...],
            "traceId": "...",
            "spanId": "..."
          }]
        }]
      }]
    }

    Returns a list of JSON-line strings ready for ``ingest_logs()``.
    """
    entries: list[dict] = []
    resource_logs = payload.get("resourceLogs") or payload.get("resource_logs") or []

    for rl in resource_logs:
        # ── Extract service name from resource attributes ────────
        resource = rl.get("resource", {})
        resource_attrs = _attributes_to_dict(resource.get("attributes"))
        service_name = (
            resource_attrs.get("service.name")
            or resource_attrs.get("service_name")
            or "unknown-service"
        )

        scope_logs = rl.get("scopeLogs") or rl.get("scope_logs") or []
        for sl in scope_logs:
            scope = sl.get("scope", {})
            scope_name = scope.get("name", "")

            log_records = sl.get("logRecords") or sl.get("log_records") or []
            for record in log_records:
                body_text = _extract_body(record)
                severity = _extract_severity(record)
                timestamp = _nano_to_datetime(
                    record.get("timeUnixNano") or record.get("time_unix_nano")
                )
                observed = _nano_to_datetime(
                    record.get("observedTimeUnixNano") or record.get("observed_time_unix_nano")
                )

                log_attrs = _attributes_to_dict(record.get("attributes"))
                trace_id = _hex_or_none(record.get("traceId") or record.get("trace_id"))
                span_id = _hex_or_none(record.get("spanId") or record.get("span_id"))

                # Build a JSON line that log_ingestor can parse
                entry: dict[str, Any] = {
                    "timestamp": (timestamp or observed or datetime.now(tz=UTC)).isoformat(),
                    "level": severity,
                    "message": body_text,
                    "service": service_name,
                    "ingestSource": "otel",
                }

                # Carry forward useful OTEL attributes
                if trace_id:
                    entry["traceId"] = trace_id
                if span_id:
                    entry["spanId"] = span_id
                if scope_name:
                    entry["scope"] = scope_name

                # Merge log record attributes (may include file, line, etc.)
                if log_attrs:
                    # Check for common attribute keys
                    if "code.filepath" in log_attrs:
                        entry["file"] = log_attrs["code.filepath"]
                    if "code.lineno" in log_attrs:
                        entry["line"] = log_attrs["code.lineno"]
                    if "exception.type" in log_attrs:
                        entry["error"] = log_attrs["exception.type"]
                    if "exception.message" in log_attrs:
                        msg = log_attrs["exception.message"]
                        entry["error"] = f"{entry.get('error', '')}: {msg}".strip(": ")
                    if "exception.stacktrace" in log_attrs:
                        entry["message"] += "\n" + log_attrs["exception.stacktrace"]
                    # Store remaining attributes
                    extra = {
                        k: v for k, v in log_attrs.items()
                        if k not in {
                            "code.filepath", "code.lineno",
                            "exception.type", "exception.message",
                            "exception.stacktrace",
                        }
                    }
                    if extra:
                        entry.update(extra)

                entries.append(entry)

    return entries


async def ingest_otel_logs(
    session: AsyncSession,
    payload: dict,
) -> dict:
    """Receive an OTLP ExportLogsServiceRequest, parse, and ingest.

    Returns summary dict with ingestion stats.
    """
    parsed_entries = parse_otlp_logs_json(payload)
    if not parsed_entries:
        return {
            "status": "completed",
            "log_records_received": 0,
            "entries_ingested": 0,
            "dropped": 0,
            "drop_reasons": {},
            "by_level": {},
            "services": [],
        }

    # Group entries by service for better log_ingestor semantics
    by_service: dict[str, list[dict]] = {}
    for entry in parsed_entries:
        svc = entry.get("service", "unknown-service")
        by_service.setdefault(svc, []).append(entry)

    total_ingested = 0
    total_dropped = 0
    combined_by_level: dict[str, int] = {}
    combined_drop_reasons: dict[str, int] = {}
    services_seen: list[str] = []

    for service_name, entries in by_service.items():
        # Convert entries back to JSON lines for the existing ingestor
        json_lines = "\n".join(json.dumps(e) for e in entries)

        stats = await ingest_logs(
            raw_text=json_lines,
            service_name=service_name,
            source="otel",
            session=session,
        )

        ingested = stats.get("entries_ingested", 0)
        dropped = stats.get("dropped", 0)
        total_ingested += ingested
        total_dropped += dropped
        services_seen.append(service_name)

        for level, count in stats.get("by_level", {}).items():
            combined_by_level[level] = combined_by_level.get(level, 0) + count
        for reason, count in stats.get("drop_reasons", {}).items():
            combined_drop_reasons[reason] = combined_drop_reasons.get(reason, 0) + count

    # Update in-memory stats
    with _otel_stats_lock:
        _otel_stats["total_requests_received"] += 1
        _otel_stats["total_log_records_received"] += len(parsed_entries)
        _otel_stats["total_entries_ingested"] += total_ingested
        _otel_stats["total_dropped"] = _otel_stats.get("total_dropped", 0) + total_dropped
        _otel_stats["last_received_at"] = datetime.now(tz=UTC).isoformat()
        for svc in services_seen:
            _otel_stats["services_seen"][svc] = (
                _otel_stats["services_seen"].get(svc, 0) + 1
            )

    return {
        "status": "completed",
        "log_records_received": len(parsed_entries),
        "entries_ingested": total_ingested,
        "dropped": total_dropped,
        "drop_reasons": combined_drop_reasons,
        "by_level": combined_by_level,
        "services": services_seen,
    }
