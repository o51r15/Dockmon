"""Dockmon entry point — starts the monitor loop and API server."""

from __future__ import annotations

import sys
from pathlib import Path

from dockmon.config import load_config
from dockmon.db import init_db, verify_tables
from dockmon.docker_client import get_client


def startup_check(config_path: str = "/app/config/config.yaml") -> None:
    """Run all Phase 0 checks: config, database, Docker connection."""
    print("=" * 50)
    print("  Dockmon — AI Container Monitor")
    print("=" * 50)
    print()

    # 1. Config
    print("[1/3] Loading configuration...")
    cfg = load_config(config_path)
    enabled = [c for c in cfg.containers if c.enabled]
    print(f"  OK — {len(enabled)} container(s) to monitor")
    for c in enabled:
        print(f"    • {c.name}")
    print()

    # 2. Database
    print("[2/3] Initializing database...")
    conn = init_db()
    tables = verify_tables(conn)
    print(f"  OK — tables: {tables}")
    conn.close()
    print()

    # 3. Docker
    print("[3/3] Connecting to Docker...")
    client = get_client()
    version = client.version()["Version"]
    containers = client.containers.list()
    print(f"  OK — Docker {version}, {len(containers)} running container(s)")

    # Check which monitored containers are actually running
    running_names = {c.name for c in containers}
    for c in enabled:
        status = "FOUND" if c.name in running_names else "NOT FOUND"
        print(f"    • {c.name}: {status}")
    print()

    print("Startup checks passed. Dockmon is ready.")
    print(f"  Mode: {'DRY RUN' if cfg.monitoring.dry_run else 'LIVE'}")
    print(f"  Poll interval: {cfg.monitoring.poll_interval_seconds}s")
    print(f"  Ollama: {cfg.ollama.base_url} ({cfg.ollama.default_model})")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config/config.yaml"
    startup_check(config_path)
