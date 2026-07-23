"""SQLite database layer for DockLlama."""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "/app/data/dockllama.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL,
    ai_status TEXT,
    confidence INTEGER,
    root_cause_category TEXT,
    summary TEXT,
    action_taken TEXT,
    log_snapshot TEXT,
    prompt_version TEXT,
    model_used TEXT,
    health_score INTEGER
);

CREATE TABLE IF NOT EXISTS cooldowns (
    container TEXT PRIMARY KEY,
    last_restart TEXT NOT NULL,
    consecutive_restarts INTEGER DEFAULT 0,
    current_cooldown_minutes INTEGER DEFAULT 5,
    alert_only_mode INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS baselines (
    container TEXT PRIMARY KEY,
    healthy_log_sample TEXT,
    captured_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_container ON events(container);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    overall_health TEXT,
    headline TEXT,
    digest_json TEXT NOT NULL,
    formatted_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_digests_date ON digests(date);

CREATE TABLE IF NOT EXISTS alert_urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode enabled."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Initialize the database with the schema."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def verify_tables(conn: sqlite3.Connection) -> dict[str, int]:
    """Return row counts for all tables."""
    tables = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        tables[name] = count
    return tables


if __name__ == "__main__":
    import tempfile
    import os

    test_path = os.path.join(tempfile.gettempdir(), "dockllama_test.db")
    print(f"Testing DB at {test_path}")

    conn = init_db(test_path)
    tables = verify_tables(conn)
    print(f"Tables created: {tables}")

    # Verify WAL mode
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    print(f"Journal mode: {mode}")

    conn.close()
    os.unlink(test_path)
    print("Test passed, cleaned up.")


def prune_old_events(conn, retention_days=90):
    """Delete events older than retention_days. Returns rows deleted."""
    import logging
    from datetime import datetime, timedelta, timezone
    logger = logging.getLogger(__name__)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    if deleted:
        logger.info("Pruned %d events older than %d days", deleted, retention_days)
    return deleted


def vacuum_db(conn):
    """Reclaim space after pruning."""
    conn.execute("VACUUM")
