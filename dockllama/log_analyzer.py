"""Log analyzer — pre-processes raw container logs into structured summaries for LLM evaluation.

Instead of dumping raw/filtered logs to the LLM, this module:
1. Parses timestamps and computes the time span of the log window
2. Counts lines by severity (INFO, WARN, ERROR, FATAL)
3. Detects container restart events (shutdown → startup sequences)
4. Deduplicates repeated error messages with counts and timestamps
5. Detects recovery patterns (errors followed by healthy operation)
6. Extracts a tail of recent unfiltered lines showing current state
7. Builds a structured text summary optimized for LLM consumption
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from dockllama.log_pipeline import strip_ansi, strip_docker_timestamp, detect_level

# Timestamp patterns for various log formats
TS_PATTERNS = [
    # PostgreSQL: "2026-07-18 12:37:56.193 UTC"
    re.compile(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"),
    # ISO 8601: "2026-07-18T12:37:56"
    re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),
]

# Restart indicators
SHUTDOWN_PATTERNS = [
    re.compile(r"received.*shutdown.*request", re.IGNORECASE),
    re.compile(r"shutting down", re.IGNORECASE),
    re.compile(r"stopping\s+(service|server|process)", re.IGNORECASE),
    re.compile(r"graceful\s+shutdown", re.IGNORECASE),
    re.compile(r"signal.*TERM|SIGTERM|SIGINT", re.IGNORECASE),
]

STARTUP_PATTERNS = [
    re.compile(r"ready to accept connections", re.IGNORECASE),
    re.compile(r"started?\s+(server|service|worker|listening)", re.IGNORECASE),
    re.compile(r"listening\s+on", re.IGNORECASE),
    re.compile(r"server\s+started", re.IGNORECASE),
    re.compile(r"initialization\s+complete", re.IGNORECASE),
    re.compile(r"starting\s+PostgreSQL", re.IGNORECASE),
    re.compile(r"database system is ready", re.IGNORECASE),
    re.compile(r"WebUI.*accessible", re.IGNORECASE),
]

# Normalize error messages for deduplication
NORMALIZE_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}[.\d]*\s*(UTC|Z)?"), "<TS>"),
    (re.compile(r"\[?\d+\]?"), ""),  # PIDs
    (re.compile(r"0x[0-9a-fA-F]+"), "<ADDR>"),  # hex addresses
    (re.compile(r"\s+"), " "),  # collapse whitespace
]


@dataclass
class ErrorPattern:
    """A deduplicated error pattern with count and timing."""
    message: str  # representative error message (first occurrence)
    normalized: str  # normalized form for grouping
    count: int = 1
    first_line_num: int = 0
    last_line_num: int = 0
    first_timestamp: str = ""
    last_timestamp: str = ""
    context: str = ""  # surrounding context (e.g., "during shutdown")


@dataclass
class LogSummary:
    """Structured summary of container logs for LLM evaluation."""
    container_name: str
    total_lines: int = 0
    time_span_human: str = ""
    first_timestamp: str = ""
    last_timestamp: str = ""
    severity_counts: dict = field(default_factory=lambda: {
        "info": 0, "warn": 0, "error": 0, "unknown": 0
    })
    restart_detected: bool = False
    restart_details: str = ""
    last_error_line_num: int = 0
    last_error_timestamp: str = ""
    last_healthy_line_num: int = 0
    last_healthy_timestamp: str = ""
    recovery_detected: bool = False
    error_patterns: list = field(default_factory=list)
    recent_tail: list = field(default_factory=list)
    # Keep raw lines + levels for the EvaluationContext
    all_lines: list = field(default_factory=list)
    all_levels: list = field(default_factory=list)

    def to_prompt(self) -> str:
        """Format as structured text for LLM consumption."""
        parts = []

        # Header
        parts.append(f"Container: {self.container_name}")
        if self.time_span_human:
            parts.append(f"Log window: {self.time_span_human} ({self.first_timestamp} to {self.last_timestamp})")
        parts.append(
            f"Lines: {self.total_lines} total | "
            f"{self.severity_counts['info']} INFO | "
            f"{self.severity_counts['warn']} WARN | "
            f"{self.severity_counts['error']} ERROR | "
            f"{self.severity_counts['unknown']} other"
        )

        # Restart info
        if self.restart_detected:
            parts.append(f"Restart detected: {self.restart_details}")

        # Error timing
        total_errors = self.severity_counts["error"] + self.severity_counts["warn"]
        if total_errors > 0:
            if self.last_error_timestamp:
                parts.append(f"Last error at: {self.last_error_timestamp} (line {self.last_error_line_num}/{self.total_lines})")
            if self.last_healthy_timestamp:
                parts.append(f"Last healthy signal at: {self.last_healthy_timestamp} (line {self.last_healthy_line_num}/{self.total_lines})")

            # Recovery
            if self.recovery_detected:
                healthy_after = self.total_lines - self.last_error_line_num
                parts.append(f"Recovery: YES — {healthy_after} clean lines after last error")
            elif self.last_error_line_num == self.total_lines:
                parts.append("Recovery: NO — errors continue through most recent lines")
            else:
                parts.append("Recovery: UNCLEAR")
        else:
            parts.append("Errors: NONE — all lines are informational or routine")

        # Error patterns
        if self.error_patterns:
            parts.append("")
            parts.append("Error patterns (deduplicated):")
            for i, ep in enumerate(self.error_patterns[:10], 1):
                loc = f"lines {ep.first_line_num}-{ep.last_line_num}" if ep.first_line_num != ep.last_line_num else f"line {ep.first_line_num}"
                parts.append(f"  {i}. [{ep.count}x] \"{ep.message[:200]}\"")
                ts_info = f"     at {loc}/{self.total_lines}"
                if ep.first_timestamp:
                    ts_info += f" ({ep.first_timestamp}"
                    if ep.last_timestamp and ep.last_timestamp != ep.first_timestamp:
                        ts_info += f" to {ep.last_timestamp}"
                    ts_info += ")"
                parts.append(ts_info)
                if ep.context:
                    parts.append(f"     Context: {ep.context}")

        # Recent tail
        if self.recent_tail:
            parts.append("")
            tail_start = self.total_lines - len(self.recent_tail) + 1
            parts.append(f"Current state (last {len(self.recent_tail)} unfiltered lines):")
            for i, line in enumerate(self.recent_tail):
                line_num = tail_start + i
                parts.append(f"  [{line_num}/{self.total_lines}] {line[:300]}")

        return "\n".join(parts)


def _parse_timestamp(line: str) -> str | None:
    """Extract a timestamp string from a log line."""
    for pat in TS_PATTERNS:
        m = pat.search(line)
        if m:
            return m.group(1).replace("T", " ")[:19]
    return None


def _parse_datetime(ts_str: str) -> datetime | None:
    """Parse a timestamp string into a datetime."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"


def _normalize_error(msg: str) -> str:
    """Normalize an error message for deduplication."""
    result = msg
    for pat, repl in NORMALIZE_PATTERNS:
        result = pat.sub(repl, result)
    return result.strip()[:150]


def analyze_logs(
    container_name: str,
    raw_logs: str,
    ignore_patterns: list[str] | None = None,
    max_lines: int = 200,
    tail_size: int = 25,
) -> LogSummary:
    """
    Analyze raw container logs and produce a structured summary.

    Unlike the old pipeline that filtered out INFO lines, this analyzes ALL lines
    to build a complete picture, then presents a summary + recent tail to the LLM.
    """
    summary = LogSummary(container_name=container_name)

    # Split and clean lines
    raw_lines = raw_logs.strip().splitlines()
    if not raw_lines:
        summary.total_lines = 0
        return summary

    # Compile ignore patterns
    compiled_ignores = []
    if ignore_patterns:
        for pat in ignore_patterns:
            try:
                compiled_ignores.append(re.compile(pat))
            except re.error:
                pass

    # Process each line
    lines = []
    levels = []
    timestamps = []
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        clean = strip_docker_timestamp(strip_ansi(raw_line)).strip()
        if not clean:
            continue

        # Check ignore patterns
        ignored = False
        for pat in compiled_ignores:
            if pat.search(clean):
                ignored = True
                break
        if ignored:
            continue

        level = detect_level(clean)
        ts = _parse_timestamp(clean)

        lines.append(clean)
        levels.append(level)
        timestamps.append(ts)

    summary.total_lines = len(lines)
    summary.all_lines = lines
    summary.all_levels = levels

    if not lines:
        return summary

    # Severity counts
    level_counts = Counter(levels)
    summary.severity_counts = {
        "info": level_counts.get("info", 0),
        "warn": level_counts.get("warn", 0),
        "error": level_counts.get("error", 0),
        "unknown": level_counts.get("unknown", 0),
    }

    # Timestamps and time span
    valid_ts = [(i, ts) for i, ts in enumerate(timestamps) if ts]
    if valid_ts:
        summary.first_timestamp = valid_ts[0][1]
        summary.last_timestamp = valid_ts[-1][1]
        first_dt = _parse_datetime(valid_ts[0][1])
        last_dt = _parse_datetime(valid_ts[-1][1])
        if first_dt and last_dt and last_dt > first_dt:
            span = (last_dt - first_dt).total_seconds()
            summary.time_span_human = _format_duration(span)

    # Find last error and last healthy signal
    for i in range(len(lines) - 1, -1, -1):
        if levels[i] in ("error", "warn") and summary.last_error_line_num == 0:
            summary.last_error_line_num = i + 1  # 1-indexed
            summary.last_error_timestamp = timestamps[i] or ""
        if levels[i] == "info" and summary.last_healthy_line_num == 0:
            summary.last_healthy_line_num = i + 1
            summary.last_healthy_timestamp = timestamps[i] or ""
        if summary.last_error_line_num and summary.last_healthy_line_num:
            break

    # Recovery detection: errors stopped and healthy lines followed
    if summary.last_error_line_num > 0:
        lines_after_last_error = summary.total_lines - summary.last_error_line_num
        # If there are at least 5 clean lines after the last error, consider it recovered
        # Also check that recent lines aren't errors
        recent_error_count = sum(
            1 for lvl in levels[-min(20, len(levels)):]
            if lvl in ("error", "warn")
        )
        summary.recovery_detected = (
            lines_after_last_error >= 5 and
            recent_error_count == 0
        )

    # Restart detection
    shutdown_line = None
    startup_line = None
    for i, line in enumerate(lines):
        for pat in SHUTDOWN_PATTERNS:
            if pat.search(line):
                shutdown_line = i
                break
        for pat in STARTUP_PATTERNS:
            if pat.search(line):
                startup_line = i
                break

    if shutdown_line is not None and startup_line is not None and startup_line > shutdown_line:
        summary.restart_detected = True
        shutdown_ts = timestamps[shutdown_line] or f"line {shutdown_line + 1}"
        startup_ts = timestamps[startup_line] or f"line {startup_line + 1}"
        summary.restart_details = f"Shutdown at {shutdown_ts}, back up at {startup_ts}"
    elif startup_line is not None and startup_line < len(lines) // 3:
        # Startup near the beginning suggests the container recently started
        startup_ts = timestamps[startup_line] or f"line {startup_line + 1}"
        summary.restart_detected = True
        summary.restart_details = f"Container started at {startup_ts}"

    # Error deduplication
    error_groups: dict[str, ErrorPattern] = {}
    for i, (line, level) in enumerate(zip(lines, levels)):
        if level not in ("error", "warn"):
            continue
        normalized = _normalize_error(line)
        if normalized in error_groups:
            ep = error_groups[normalized]
            ep.count += 1
            ep.last_line_num = i + 1
            ep.last_timestamp = timestamps[i] or ""
        else:
            ep = ErrorPattern(
                message=line[:250],
                normalized=normalized,
                count=1,
                first_line_num=i + 1,
                last_line_num=i + 1,
                first_timestamp=timestamps[i] or "",
                last_timestamp=timestamps[i] or "",
            )
            # Check if this error is during a shutdown
            if shutdown_line is not None and abs(i - shutdown_line) <= 10:
                ep.context = "During shutdown sequence"
            error_groups[normalized] = ep

    # Sort by count descending
    summary.error_patterns = sorted(error_groups.values(), key=lambda e: e.count, reverse=True)

    # Recent tail (unfiltered, last N lines)
    summary.recent_tail = lines[-tail_size:]

    return summary
