"""Log ingestion pipeline — pre-filtering, level detection, and buffering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ANSI escape code stripper
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Docker timestamp prefix: 2026-07-18T00:50:17.334077939Z
DOCKER_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s+")

# Common log level patterns across formats:
# Go (bitmagnet): \x1b[34mINFO\x1b[0m or plain INFO/WARN/ERROR
# Python logging: INFO, WARNING, ERROR, CRITICAL
# Postgres: LOG, WARNING, ERROR, FATAL, PANIC
# Syslog-style: info, warn, error, crit
# Nginx: [error], [warn]
# Generic: level=info, level=error
LEVEL_PATTERNS = [
    # After ANSI strip: "INFO\t", "WARN\t", "ERROR\t" (Go-style tab-delimited)
    (re.compile(r"^(INFO|DEBUG)\b"), "info"),
    (re.compile(r"^(WARN|WARNING)\b"), "warn"),
    (re.compile(r"^(ERROR|FATAL|PANIC|CRITICAL)\b"), "error"),
    # Postgres: "2026-07-18 00:48:26.225 UTC [27] LOG:"
    (re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}.*\bLOG:"), "info"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}.*\bWARNING:"), "warn"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}.*\b(ERROR|FATAL|PANIC):"), "error"),
    # Python logging: "2026-07-18 00:48:26 - module - INFO -"
    (re.compile(r"\b(INFO|DEBUG)\s*[-:]"), "info"),
    (re.compile(r"\b(WARNING|WARN)\s*[-:]"), "warn"),
    (re.compile(r"\b(ERROR|CRITICAL|FATAL)\s*[-:]"), "error"),
    # Nginx bracket style: [error], [warn]
    (re.compile(r"\[(error|crit|alert|emerg)\]", re.IGNORECASE), "error"),
    (re.compile(r"\[warn(ing)?\]", re.IGNORECASE), "warn"),
    (re.compile(r"\[(info|notice|debug)\]", re.IGNORECASE), "info"),
    # Key=value style: level=info
    (re.compile(r"\blevel=(error|fatal|panic|critical)", re.IGNORECASE), "error"),
    (re.compile(r"\blevel=(warn|warning)", re.IGNORECASE), "warn"),
    (re.compile(r"\blevel=(info|debug|trace)", re.IGNORECASE), "info"),
]


@dataclass
class LogBatch:
    """Result of processing a container's logs."""
    container_name: str
    total_lines: int = 0
    filtered_lines: list[str] = field(default_factory=list)
    dropped_by_ignore: int = 0
    dropped_by_level: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ANSI_RE.sub("", text)


def strip_docker_timestamp(text: str) -> str:
    """Remove Docker-injected RFC3339Nano timestamp prefix."""
    return DOCKER_TS_RE.sub("", text)


def detect_level(line: str) -> str:
    """Detect log level from a line. Returns 'info', 'warn', 'error', or 'unknown'."""
    clean = strip_docker_timestamp(strip_ansi(line)).strip()
    for pattern, level in LEVEL_PATTERNS:
        if pattern.search(clean):
            return level
    return "unknown"


def process_logs(
    container_name: str,
    raw_logs: str,
    ignore_patterns: list[str] | None = None,
    max_lines: int = 200,
) -> LogBatch:
    """
    Process raw container logs through the pipeline:
    1. Split into lines
    2. Strip ANSI codes for matching (preserve originals for context)
    3. Drop lines matching ignore_patterns
    4. Drop lines at INFO/DEBUG level (only forward WARN/ERROR/unknown)
    5. Cap at max_lines (keep most recent)
    """
    batch = LogBatch(container_name=container_name)
    lines = raw_logs.strip().splitlines()
    batch.total_lines = len(lines)

    # Compile ignore patterns
    compiled_ignores = []
    if ignore_patterns:
        for pat in ignore_patterns:
            try:
                compiled_ignores.append(re.compile(pat))
            except re.error:
                pass  # Skip invalid patterns

    kept: list[str] = []

    for line in lines:
        if not line.strip():
            continue

        clean = strip_docker_timestamp(strip_ansi(line)).strip()

        # Step 1: Check ignore patterns against cleaned line
        ignored = False
        for pat in compiled_ignores:
            if pat.search(clean):
                ignored = True
                break
        if ignored:
            batch.dropped_by_ignore += 1
            continue

        # Step 2: Log-level pre-filter — only forward warn/error/unknown
        level = detect_level(clean)
        if level == "info":
            batch.dropped_by_level += 1
            continue

        kept.append(clean)

    # Step 3: Cap at max_lines (keep tail — most recent)
    if len(kept) > max_lines:
        kept = kept[-max_lines:]

    batch.filtered_lines = kept
    return batch


if __name__ == "__main__":
    # Self-test with synthetic logs
    test_logs = """
\x1b[34mINFO\x1b[0m\tprowlarr\tprowlarr/crawler.go:180\tprowlarr: crawling indexer\t{"indexer_id": 109}
\x1b[33mWARN\x1b[0m\tprowlarr\tprowlarr/crawler.go:187\tprowlarr: search failed\t{"indexer_id": 98, "error": "prowlarr: API returned status 400 for search"}
\x1b[34mINFO\x1b[0m\tprowlarr\tprowlarr/crawler.go:293\tprowlarr: crawl complete\t{"indexer_id": 94, "imported": 30}
\x1b[31mERROR\x1b[0m\tapp\tserver.go:55\tfailed to connect to database\t{"error": "connection refused"}
2026-07-18 01:23:37 UTC [27] LOG:  checkpoint complete: wrote 97 buffers
2026-07-18 01:23:37 UTC [27] WARNING:  could not write to WAL
2026-07-18 01:23:37 UTC [27] ERROR:  out of shared memory
some random line with no level indicator
    """.strip()

    batch = process_logs(
        "test-container",
        test_logs,
        ignore_patterns=["checkpoint"],
        max_lines=200,
    )
    print(f"Container: {batch.container_name}")
    print(f"Total lines: {batch.total_lines}")
    print(f"Dropped (ignore): {batch.dropped_by_ignore}")
    print(f"Dropped (level): {batch.dropped_by_level}")
    print(f"Forwarded: {len(batch.filtered_lines)}")
    print("---")
    for line in batch.filtered_lines:
        level = detect_level(line)
        print(f"  [{level:7s}] {line[:100]}")
