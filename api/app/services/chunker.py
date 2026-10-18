"""
RootOps V2 — Code-Aware Text Chunker

Splits source files into semantically meaningful fragments for embedding.
Uses simple heuristics to detect function/class boundaries, falling back
to a sliding-window approach for unrecognised formats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ── Language patterns for boundary detection ─────────────────────
# Maps file extensions to regex patterns that mark logical boundaries
# (function defs, class defs, etc.)
_BOUNDARY_PATTERNS: dict[str, re.Pattern] = {
    "python": re.compile(
        r"^(class\s+\w|def\s+\w|async\s+def\s+\w)", re.MULTILINE
    ),
    "javascript": re.compile(
        r"^(function\s+\w|class\s+\w|const\s+\w+\s*=\s*(?:async\s+)?\(|export\s+(?:default\s+)?(?:function|class))",
        re.MULTILINE,
    ),
    "typescript": re.compile(
        r"^(function\s+\w|class\s+\w|const\s+\w+\s*=\s*(?:async\s+)?\(|export\s+(?:default\s+)?(?:function|class)|interface\s+\w)",
        re.MULTILINE,
    ),
    "go": re.compile(r"^(func\s+|type\s+\w+\s+struct)", re.MULTILINE),
    "java": re.compile(
        r"^(\s*(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|void|int|String|boolean)\s+\w)",
        re.MULTILINE,
    ),
    "rust": re.compile(
        r"^(pub\s+)?(?:fn\s+|struct\s+|impl\s+|trait\s+|enum\s+)",
        re.MULTILINE,
    ),
}

_EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}

# File extensions we consider as source code
SUPPORTED_EXTENSIONS: set[str] = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".sh", ".bash", ".yaml", ".yml", ".json",
    ".toml", ".cfg", ".ini", ".md", ".rst", ".txt", ".sql",
    ".html", ".css", ".scss", ".tf", ".hcl",
}


@dataclass
class ChunkResult:
    """A single chunk extracted from a source file."""
    content: str
    start_line: int
    end_line: int
    language: str | None = None
    file_path: str = ""


@dataclass
class ChunkerConfig:
    """Configuration for the chunking strategy."""
    max_chunk_lines: int = 60
    min_chunk_lines: int = 5
    overlap_lines: int = 10
    max_file_size_bytes: int = 1_000_000  # skip files > 1 MB


def detect_language(file_path: str) -> str | None:
    """Detect programming language from file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANGUAGE.get(ext)


def is_supported_file(file_path: str) -> bool:
    """Check if a file should be chunked based on its extension."""
    ext = Path(file_path).suffix.lower()
    return ext in SUPPORTED_EXTENSIONS


def chunk_file(
    content: str,
    file_path: str,
    config: ChunkerConfig | None = None,
) -> list[ChunkResult]:
    """Split a file's content into semantic chunks.

    Strategy:
    1. If we recognise the language, split on function/class boundaries.
    2. If not, use a sliding-window approach.

    Args:
        content: Full text content of the file.
        file_path: Path for language detection and metadata.
        config: Optional chunking configuration.

    Returns:
        List of ChunkResult objects.
    """
    if not content.strip():
        return []

    cfg = config or ChunkerConfig()
    language = detect_language(file_path)
    lines = content.splitlines(keepends=True)

    # Skip overly large files
    if len(content.encode("utf-8", errors="ignore")) > cfg.max_file_size_bytes:
        return []

    # Try boundary-based chunking for known languages
    if language and language in _BOUNDARY_PATTERNS:
        chunks = _chunk_by_boundaries(lines, language, file_path, cfg)
        if chunks:
            return chunks

    # Fallback: sliding window
    return _chunk_sliding_window(lines, language, file_path, cfg)


def _chunk_by_boundaries(
    lines: list[str],
    language: str,
    file_path: str,
    cfg: ChunkerConfig,
) -> list[ChunkResult]:
    """Split on function/class boundaries detected by regex."""
    pattern = _BOUNDARY_PATTERNS[language]
    full_text = "".join(lines)

    # Find line numbers of all boundary matches
    boundary_lines: list[int] = []
    for match in pattern.finditer(full_text):
        line_no = full_text[: match.start()].count("\n")
        boundary_lines.append(line_no)

    if not boundary_lines:
        return []

    chunks: list[ChunkResult] = []

    # If there's content before the first boundary (imports, etc.), include it
    if boundary_lines[0] > 0:
        preamble = "".join(lines[: boundary_lines[0]])
        if preamble.strip() and len(preamble.splitlines()) >= cfg.min_chunk_lines:
            chunks.append(
                ChunkResult(
                    content=preamble.rstrip(),
                    start_line=1,
                    end_line=boundary_lines[0],
                    language=language,
                    file_path=file_path,
                )
            )

    # Create chunks between boundaries
    for i, start in enumerate(boundary_lines):
        end = boundary_lines[i + 1] if i + 1 < len(boundary_lines) else len(lines)
        chunk_lines = lines[start:end]
        chunk_text = "".join(chunk_lines).rstrip()

        if not chunk_text.strip():
            continue

        # If chunk is too large, sub-split with sliding window
        if len(chunk_lines) > cfg.max_chunk_lines:
            sub_chunks = _chunk_sliding_window(
                chunk_lines, language, file_path, cfg
            )
            # Adjust line numbers relative to the full file
            for sc in sub_chunks:
                sc.start_line += start
                sc.end_line += start
            chunks.extend(sub_chunks)
        else:
            chunks.append(
                ChunkResult(
                    content=chunk_text,
                    start_line=start + 1,  # 1-indexed
                    end_line=end,
                    language=language,
                    file_path=file_path,
                )
            )

    return chunks


def _chunk_sliding_window(
    lines: list[str],
    language: str | None,
    file_path: str,
    cfg: ChunkerConfig,
) -> list[ChunkResult]:
    """Sliding-window chunking with overlap."""
    chunks: list[ChunkResult] = []
    total = len(lines)
    step = cfg.max_chunk_lines - cfg.overlap_lines

    step = max(step, 1)

    i = 0
    while i < total:
        end = min(i + cfg.max_chunk_lines, total)
        chunk_text = "".join(lines[i:end]).rstrip()

        if chunk_text.strip() and len(chunk_text.splitlines()) >= cfg.min_chunk_lines:
            chunks.append(
                ChunkResult(
                    content=chunk_text,
                    start_line=i + 1,  # 1-indexed
                    end_line=end,
                    language=language,
                    file_path=file_path,
                )
            )

        if end >= total:
            break
        i += step

    return chunks
