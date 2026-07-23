"""DockLlama entry point — runs startup checks, monitor loop, and web server."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from datetime import timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from dockllama.config import load_config, DockLlamaConfig
from dockllama.db import init_db, verify_tables, prune_old_events, vacuum_db
from dockllama.docker_client import get_client, get_logs, list_containers
from dockllama.log_pipeline import process_logs
from dockllama.log_analyzer import analyze_logs
from dockllama.ai_engine import evaluate, EvaluationContext, EvaluationResult
from dockllama.actions import execute_action
from dockllama.alerts import (
    init_alerts, alert_restart, alert_dry_run, alert_escalation,
    alert_cooldown_skip, alert_error,
)
from dockllama.api.routes import router as api_router, set_config
from dockllama.api.events import router as sse_router, publish
from dockllama.digest import send_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dockllama")

# Graceful shutdown
_shutdown = asyncio.Event()


def _handle_signal(sig, frame):
    logger.info("Received %s, shutting down...", signal.Signals(sig).name)
    _shutdown.set()


def create_app(cfg: DockLlamaConfig) -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="DockLlama", version="0.1.0")
    set_config(cfg)
    app.include_router(api_router)
    app.include_router(sse_router)

    # Serve frontend
    frontend_dir = Path(__file__).parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app


def startup_check(cfg: DockLlamaConfig) -> None:
    """Run all startup checks."""
    print("=" * 50)
    print("  DockLlama -- AI Container Monitor")
    print("=" * 50)
    print()

    enabled = [c for c in cfg.containers if c.enabled]
    logger.info("Config loaded: %d container(s) to monitor", len(enabled))
    for c in enabled:
        logger.info("  - %s", c.name)

    conn = init_db(cfg.monitoring.db_path)
    tables = verify_tables(conn)
    logger.info("Database OK: %s", tables)
    pruned = prune_old_events(conn, cfg.monitoring.retention_days)
    if pruned:
        vacuum_db(conn)
        logger.info("DB maintenance: pruned %d old events, vacuumed", pruned)
    # Load persisted alert URLs from DB, merge with config
    from dockllama.alerts import load_alert_urls
    db_urls = load_alert_urls(conn)
    config_urls = cfg.alerts.urls or []
    # Merge: DB URLs take priority, config URLs added if not already present
    merged_urls = list(db_urls)
    for u in config_urls:
        if u not in merged_urls:
            merged_urls.append(u)
    if merged_urls != config_urls:
        cfg.alerts.urls = merged_urls
    init_alerts(merged_urls)
    conn.close()

    client = get_client()
    version = client.version()["Version"]
    containers = client.containers.list()
    logger.info("Docker %s: %d running container(s)", version, len(containers))

    running_names = {c.name for c in containers}
    for c in enabled:
        status = "FOUND" if c.name in running_names else "NOT FOUND"
        logger.info("  - %s: %s", c.name, status)

    # Ollama connectivity
    import httpx
    try:
        resp = httpx.get(f"{cfg.ollama.base_url}/api/tags", timeout=10)
        models = [m["name"] for m in resp.json().get("models", [])]
        logger.info("Ollama OK: %d model(s) available", len(models))
        if cfg.ollama.default_model not in models:
            logger.warning("  Model '%s' not found on server!", cfg.ollama.default_model)
    except Exception as e:
        logger.warning("Ollama unreachable at %s: %s (will retry during monitoring)", cfg.ollama.base_url, e)


    logger.info("Mode: %s", "DRY RUN" if cfg.monitoring.dry_run else "LIVE")
    logger.info("Poll interval: %ds", cfg.monitoring.poll_interval_seconds)
    logger.info("Ollama: %s (%s)", cfg.ollama.base_url, cfg.ollama.default_model)
    logger.info("Cooldowns: %dm initial, %dx backoff, %dm max, %d max restarts/hr",
                cfg.cooldowns.initial_minutes, cfg.cooldowns.backoff_multiplier,
                cfg.cooldowns.max_cooldown_minutes, cfg.cooldowns.max_restarts_per_hour)


async def monitor_cycle(cfg: DockLlamaConfig) -> None:
    """Run one full monitoring cycle across all enabled containers."""
    client = get_client()
    enabled = [c for c in cfg.containers if c.enabled]
    running = list_containers(client, [c.name for c in enabled])
    running_map = {c.name: c for c in running}

    conn = init_db(cfg.monitoring.db_path)

    for container_cfg in enabled:
        if container_cfg.name not in running_map:
            logger.warning("Container %s not running, skipping", container_cfg.name)
            continue

        try:
            await _process_container(container_cfg, running_map[container_cfg.name], cfg, conn)
        except Exception:
            logger.exception("Error processing container %s", container_cfg.name)

    conn.close()


async def _process_container(container_cfg, container, cfg: DockLlamaConfig, conn) -> None:
    """Process a single container: logs -> analyze -> evaluate -> act -> alert."""
    # 1. Grab logs
    raw_logs = get_logs(container, tail=cfg.monitoring.log_lines_per_check)

    # 2. Analyze with structured preprocessor (v5)
    summary = analyze_logs(
        container_name=container_cfg.name,
        raw_logs=raw_logs,
        ignore_patterns=container_cfg.ignore_patterns,
        max_lines=cfg.monitoring.log_lines_per_check,
    )

    # Also run old filter for backward compat (log snapshot, baseline)
    batch = process_logs(
        container_name=container_cfg.name,
        raw_logs=raw_logs,
        ignore_patterns=container_cfg.ignore_patterns,
        max_lines=cfg.monitoring.log_lines_per_check,
    )

    logger.info(
        "[%s] %d lines analyzed | %d INFO, %d WARN, %d ERROR | recovery=%s",
        container_cfg.name,
        summary.total_lines,
        summary.severity_counts["info"],
        summary.severity_counts["warn"],
        summary.severity_counts["error"],
        summary.recovery_detected,
    )

    # 3. Skip LLM if all logs were filtered (nothing meaningful to evaluate)
    if summary.total_lines == 0:
        logger.info("[%s] All log lines matched ignore patterns — auto-healthy", container_cfg.name)
        result = EvaluationResult(
            status="healthy",
            health_score=95,
            confidence=90,
            summary="All log output is known noise (filtered by ignore patterns). No meaningful logs to evaluate.",
            root_cause_category="none",
            error_origin="none",
            restart_would_help=False,
            restart_reasoning="No issues detected — all logs are expected noise.",
            recommended_action="none",
        )
        prompt_version = "auto-healthy"

        # Log, store, publish, then return early
        logger.info(
            "[%s] -> %s [score=%d] (auto-healthy, no LLM call)",
            container_cfg.name, result.status.upper(), result.health_score,
        )
        log_snapshot = "(all lines filtered by ignore patterns)"
        conn.execute(
            """INSERT INTO events
               (container, event_type, ai_status, confidence, root_cause_category,
                summary, action_taken, log_snapshot, prompt_version, model_used, health_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                container_cfg.name, "evaluation", result.status, result.confidence,
                result.root_cause_category, result.summary, result.recommended_action,
                log_snapshot, prompt_version, "none", result.health_score,
            ),
        )
        conn.commit()
        publish("evaluation", {
            "container": container_cfg.name,
            "status": result.status,
            "health_score": result.health_score,
            "confidence": result.confidence,
            "root_cause_category": result.root_cause_category,
            "error_origin": result.error_origin,
            "summary": result.summary,
            "restart_would_help": result.restart_would_help,
            "restart_reasoning": result.restart_reasoning,
            "recommended_action": result.recommended_action,
        })
        return

    # 3b. Evaluate with AI
    model = container_cfg.model_override or cfg.ollama.default_model

    baseline = None
    row = conn.execute(
        "SELECT healthy_log_sample FROM baselines WHERE container = ?",
        (container_cfg.name,),
    ).fetchone()
    if row:
        baseline = row[0]

    ctx = EvaluationContext(
        container_name=container_cfg.name,
        filtered_lines=batch.filtered_lines,
        model=model,
        structured_summary=summary.to_prompt(),
        baseline_sample=baseline,
    )

    result, prompt_version = await evaluate(ctx, cfg.ollama)

    # 4. Log the evaluation
    logger.info(
        "[%s] -> %s [score=%d] (confidence=%d, origin=%s, category=%s, restart_helps=%s, action=%s): %s",
        container_cfg.name,
        result.status.upper(),
        result.health_score,
        result.confidence,
        result.error_origin,
        result.root_cause_category,
        result.restart_would_help,
        result.recommended_action,
        result.summary,
    )
    if result.restart_reasoning:
        logger.info("[%s] Restart reasoning: %s", container_cfg.name, result.restart_reasoning)

    # 5. Store evaluation event
    log_snapshot = "\n".join(batch.filtered_lines[:50])
    conn.execute(
        """INSERT INTO events
           (container, event_type, ai_status, confidence, root_cause_category,
            summary, action_taken, log_snapshot, prompt_version, model_used, health_score)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            container_cfg.name, "evaluation", result.status, result.confidence,
            result.root_cause_category, result.summary, result.recommended_action,
            log_snapshot, prompt_version, model, result.health_score,
        ),
    )
    conn.commit()

    # Publish to SSE
    publish("evaluation", {
        "container": container_cfg.name,
        "status": result.status,
        "health_score": result.health_score,
        "confidence": result.confidence,
        "root_cause_category": result.root_cause_category,
        "error_origin": result.error_origin,
        "summary": result.summary,
        "restart_would_help": result.restart_would_help,
        "restart_reasoning": result.restart_reasoning,
        "recommended_action": result.recommended_action,
    })

    # 6. Execute action
    # Resolve compose group members
    group = container_cfg.compose_group
    group_names = None
    if group:
        group_names = [c.name for c in cfg.containers if c.enabled and c.compose_group == group]

    action = execute_action(
        result=result, container=container, conn=conn,
        cooldown_cfg=cfg.cooldowns, dry_run=cfg.monitoring.dry_run,
        log_snapshot=log_snapshot,
        compose_group=group,
        group_container_names=group_names,
    )

    if action.action_taken != "none":
        logger.info("[%s] Action: %s -- %s", container_cfg.name, action.action_taken, action.message)

        publish("action", {
            "container": container_cfg.name,
            "action": action.action_taken,
            "success": action.success,
            "message": action.message,
        })

    # 7. Send alerts
    if action.action_taken == "restart":
        alert_restart(container_cfg.name, result, action)
    elif action.action_taken == "dry_run_restart":
        alert_dry_run(container_cfg.name, result)
    elif action.action_taken == "alert_only":
        alert_escalation(container_cfg.name,
                         conn.execute("SELECT consecutive_restarts FROM cooldowns WHERE container = ?",
                                      (container_cfg.name,)).fetchone()[0])
    elif action.action_taken == "cooldown_skip":
        alert_cooldown_skip(container_cfg.name, 0)

    # 8. Store action event
    if action.action_taken != "none":
        conn.execute(
            """INSERT INTO events
               (container, event_type, ai_status, confidence, root_cause_category,
                summary, action_taken, log_snapshot, model_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                container_cfg.name, "action", result.status, result.confidence,
                result.root_cause_category, action.message, action.action_taken,
                action.log_snapshot[:2000] if action.log_snapshot else "", model,
            ),
        )
        conn.commit()

    # 9. Capture baseline
    if result.status in ("healthy", "degraded") and result.confidence >= 80 and baseline is None:
        baseline_logs = get_logs(container, tail=30)
        conn.execute(
            "INSERT OR REPLACE INTO baselines (container, healthy_log_sample) VALUES (?, ?)",
            (container_cfg.name, baseline_logs[:2000]),
        )
        conn.commit()
        logger.info("[%s] Baseline captured", container_cfg.name)


async def run(cfg: DockLlamaConfig) -> None:
    """Run the monitor loop and web server concurrently."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    startup_check(cfg)

    # Start web server
    app = create_app(cfg)
    web_config = uvicorn.Config(app, host="0.0.0.0", port=8556, log_level="warning")
    server = uvicorn.Server(web_config)

    async def run_server():
        await server.serve()

    async def run_monitor():
        logger.info("Starting monitor loop...")
        while not _shutdown.is_set():
            try:
                await monitor_cycle(cfg)
            except Exception:
                logger.exception("Error in monitor cycle")
                alert_error("Monitor cycle failed. Check logs for details.")
            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=cfg.monitoring.poll_interval_seconds)
                break
            except asyncio.TimeoutError:
                pass
        logger.info("Monitor loop stopped.")
        server.should_exit = True

    async def run_digest_scheduler():
        """Run the daily digest on a cron-like schedule."""
        if not cfg.digest.enabled:
            logger.info("Digest disabled, scheduler not started.")
            return

        # Parse cron hour/minute from schedule_cron (e.g. "0 7 * * *")
        parts = cfg.digest.schedule_cron.split()
        cron_minute = int(parts[0]) if len(parts) > 0 else 0
        cron_hour = int(parts[1]) if len(parts) > 1 else 7

        logger.info("Digest scheduler: will run daily at %02d:%02d UTC", cron_hour, cron_minute)

        while not _shutdown.is_set():
            from datetime import datetime as dt, timezone as tz
            now = dt.now(tz.utc)
            # Calculate next run
            target = now.replace(hour=cron_hour, minute=cron_minute, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            logger.info("Next digest in %.0f seconds (%s UTC)", wait_secs, target.strftime("%Y-%m-%d %H:%M"))

            try:
                await asyncio.wait_for(_shutdown.wait(), timeout=wait_secs)
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # time to run digest

            try:
                conn = init_db(cfg.monitoring.db_path)
                digest = await send_digest(cfg, conn)
                # Daily maintenance after digest
                pruned = prune_old_events(conn, cfg.monitoring.retention_days)
                if pruned:
                    vacuum_db(conn)
                conn.close()
                publish("digest", {"overall_health": digest.get("overall_health", "unknown"),
                                   "headline": digest.get("headline", "")})
                logger.info("Daily digest completed.")
            except Exception:
                logger.exception("Digest failed")

        logger.info("Digest scheduler stopped.")

    logger.info("Web UI: http://0.0.0.0:8556")
    await asyncio.gather(run_server(), run_monitor(), run_digest_scheduler())
    logger.info("DockLlama stopped.")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/app/config/config.yaml"
    cfg = load_config(config_path)
    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
