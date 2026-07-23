"""FastAPI routes for the DockLlama web interface."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from dockllama.config import DockLlamaConfig
from dockllama.db import init_db
from dockllama.docker_client import get_client, get_logs, list_containers
from dockllama.log_pipeline import process_logs
from dockllama.ai_engine import evaluate, EvaluationContext

router = APIRouter(prefix="/api")

# Set by main.py at startup
_cfg: DockLlamaConfig | None = None


def set_config(cfg: DockLlamaConfig) -> None:
    global _cfg
    _cfg = cfg


def _get_cfg() -> DockLlamaConfig:
    if _cfg is None:
        raise HTTPException(500, "Config not initialized")
    return _cfg


# --- Models ---

class ContainerStatus(BaseModel):
    name: str
    running: bool
    image: str
    status: str
    last_evaluation: Optional[dict] = None


class EvalRequest(BaseModel):
    lines: Optional[list[str]] = None
    tail: int = 200


class EventRecord(BaseModel):
    id: int
    container: str
    timestamp: str
    event_type: str
    ai_status: Optional[str]
    confidence: Optional[int]
    root_cause_category: Optional[str]
    summary: Optional[str]
    action_taken: Optional[str]
    model_used: Optional[str]


# --- Endpoints ---

@router.get("/containers")
async def get_containers() -> list[ContainerStatus]:
    """List all monitored containers with their latest evaluation."""
    cfg = _get_cfg()
    client = get_client()
    running = {c.name: c for c in client.containers.list(all=True)}
    conn = init_db(cfg.monitoring.db_path)

    result = []
    enabled_names = {c.name for c in cfg.containers if c.enabled}

    for container_cfg in cfg.containers:
        if not container_cfg.enabled:
            continue

        docker_container = running.get(container_cfg.name)
        image = ""
        container_status = "not found"
        is_running = False

        if docker_container:
            image = docker_container.image.tags[0] if docker_container.image.tags else "untagged"
            container_status = docker_container.status
            is_running = container_status == "running"

        # Get latest evaluation
        row = conn.execute(
            """SELECT ai_status, confidence, root_cause_category, summary,
                      action_taken, timestamp, model_used, health_score
               FROM events WHERE container = ? AND event_type = 'evaluation'
               ORDER BY id DESC LIMIT 1""",
            (container_cfg.name,),
        ).fetchone()

        last_eval = None
        if row:
            last_eval = {
                "ai_status": row[0],
                "confidence": row[1],
                "root_cause_category": row[2],
                "summary": row[3],
                "action_taken": row[4],
                "timestamp": row[5],
                "model_used": row[6],
                "health_score": row[7],
            }

        result.append(ContainerStatus(
            name=container_cfg.name,
            running=is_running,
            image=image,
            status=container_status,
            last_evaluation=last_eval,
        ))

    conn.close()
    return result


@router.get("/containers/{name}/logs")
async def get_container_logs(name: str, tail: int = Query(200, ge=1, le=2000)):
    """Fetch recent logs from a container, with pre-filtering applied."""
    cfg = _get_cfg()
    client = get_client()
    matches = [c for c in client.containers.list() if c.name == name]
    if not matches:
        raise HTTPException(404, f"Container '{name}' not running")

    container = matches[0]
    raw = get_logs(container, tail=tail)

    # Find ignore patterns from config
    ignore_patterns = []
    for c in cfg.containers:
        if c.name == name:
            ignore_patterns = c.ignore_patterns
            break

    batch = process_logs(name, raw, ignore_patterns=ignore_patterns, max_lines=tail)

    return {
        "container": name,
        "total_lines": batch.total_lines,
        "filtered_count": len(batch.filtered_lines),
        "dropped_ignore": batch.dropped_by_ignore,
        "dropped_level": batch.dropped_by_level,
        "lines": batch.filtered_lines,
        "raw_lines": raw.strip().splitlines()[-tail:],
    }


@router.post("/containers/{name}/evaluate")
async def evaluate_container(name: str, req: EvalRequest):
    """Trigger an on-demand AI evaluation of a container's logs."""
    cfg = _get_cfg()
    client = get_client()
    matches = [c for c in client.containers.list() if c.name == name]
    if not matches:
        raise HTTPException(404, f"Container '{name}' not running")

    container = matches[0]

    if req.lines:
        filtered_lines = req.lines
    else:
        raw = get_logs(container, tail=req.tail)
        ignore_patterns = []
        for c in cfg.containers:
            if c.name == name:
                ignore_patterns = c.ignore_patterns
                break
        batch = process_logs(name, raw, ignore_patterns=ignore_patterns, max_lines=req.tail)
        filtered_lines = batch.filtered_lines

    model = cfg.ollama.default_model
    for c in cfg.containers:
        if c.name == name and c.model_override:
            model = c.model_override
            break

    conn = init_db(cfg.monitoring.db_path)
    baseline = None
    row = conn.execute(
        "SELECT healthy_log_sample FROM baselines WHERE container = ?", (name,)
    ).fetchone()
    if row:
        baseline = row[0]
    conn.close()

    ctx = EvaluationContext(
        container_name=name,
        filtered_lines=filtered_lines,
        model=model,
        baseline_sample=baseline,
    )

    start = time.time()
    result, prompt_version = await evaluate(ctx, cfg.ollama)
    elapsed = round(time.time() - start, 2)

    return {
        "container": name,
        "status": result.status,
        "health_score": result.health_score,
        "confidence": result.confidence,
        "root_cause_category": result.root_cause_category,
        "summary": result.summary,
        "recommended_action": result.recommended_action,
        "model": model,
        "prompt_version": prompt_version,
        "eval_time_seconds": elapsed,
        "lines_evaluated": len(filtered_lines),
    }


@router.get("/events")
async def get_events(
    container: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[EventRecord]:
    """Paginated event history with optional filters."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)

    query = "SELECT id, container, timestamp, event_type, ai_status, confidence, root_cause_category, summary, action_taken, model_used FROM events WHERE 1=1"
    params: list = []

    if container:
        query += " AND container = ?"
        params.append(container)
    if event_type:
        query += " AND event_type = ?"
        params.append(event_type)

    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [
        EventRecord(
            id=r[0], container=r[1], timestamp=r[2], event_type=r[3],
            ai_status=r[4], confidence=r[5], root_cause_category=r[6],
            summary=r[7], action_taken=r[8], model_used=r[9],
        )
        for r in rows
    ]


@router.get("/events/{container}/restarts")
async def get_restart_history(container: str, limit: int = Query(20, ge=1, le=100)):
    """Restart history with stored log snapshots and AI reasoning."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)

    rows = conn.execute(
        """SELECT id, timestamp, ai_status, confidence, root_cause_category,
                  summary, action_taken, log_snapshot, model_used
           FROM events WHERE container = ? AND action_taken IN ('restart', 'dry_run_restart')
           ORDER BY id DESC LIMIT ?""",
        (container, limit),
    ).fetchall()
    conn.close()

    return [
        {
            "id": r[0], "timestamp": r[1], "ai_status": r[2], "confidence": r[3],
            "root_cause_category": r[4], "summary": r[5], "action_taken": r[6],
            "log_snapshot": r[7], "model_used": r[8],
        }
        for r in rows
    ]


@router.post("/digest")
async def trigger_digest():
    """Trigger an on-demand daily digest."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        from dockllama.digest import send_digest
        digest = await send_digest(cfg, conn)
        return digest
    except Exception as e:
        raise HTTPException(500, f"Digest generation failed: {e}")
    finally:
        conn.close()



@router.get("/trends")
async def get_trends(container: Optional[str] = None):
    """7-day and 30-day health trends per container."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        from dockllama.trends import get_fleet_trends, get_container_trends
        if container:
            result = get_container_trends(conn, container)
        else:
            names = [c.name for c in cfg.containers if c.enabled]
            result = get_fleet_trends(conn, names)
        return result
    except Exception as e:
        raise HTTPException(500, f"Trend calculation failed: {e}")
    finally:
        conn.close()


@router.get("/config")
async def get_config():
    """Current running configuration (sanitized)."""
    cfg = _get_cfg()
    return {
        "ollama": {
            "base_url": cfg.ollama.base_url,
            "default_model": cfg.ollama.default_model,
            "digest_model": cfg.ollama.digest_model,
            "timeout_seconds": cfg.ollama.timeout_seconds,
        },
        "monitoring": {
            "poll_interval_seconds": cfg.monitoring.poll_interval_seconds,
            "log_lines_per_check": cfg.monitoring.log_lines_per_check,
            "dry_run": cfg.monitoring.dry_run,
        },
        "containers": [
            {"name": c.name, "enabled": c.enabled, "model_override": c.model_override,
             "compose_group": c.compose_group}
            for c in cfg.containers
        ],
        "cooldowns": {
            "initial_minutes": cfg.cooldowns.initial_minutes,
            "backoff_multiplier": cfg.cooldowns.backoff_multiplier,
            "max_cooldown_minutes": cfg.cooldowns.max_cooldown_minutes,
            "max_restarts_per_hour": cfg.cooldowns.max_restarts_per_hour,
        },
        "digest": {
            "enabled": cfg.digest.enabled,
            "schedule_cron": cfg.digest.schedule_cron,
        },
    }


@router.get("/health")
async def health_check():
    """System health: Docker, Ollama, DB."""
    cfg = _get_cfg()
    checks = {"docker": False, "ollama": False, "database": False}

    # Docker
    try:
        client = get_client()
        client.ping()
        checks["docker"] = True
    except Exception:
        pass

    # Ollama
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{cfg.ollama.base_url}/api/tags")
            checks["ollama"] = r.status_code == 200
    except Exception:
        pass

    # Database
    try:
        conn = init_db(cfg.monitoring.db_path)
        conn.execute("SELECT 1")
        checks["database"] = True
        conn.close()
    except Exception:
        pass

    all_ok = all(checks.values())
    return {"healthy": all_ok, "checks": checks}

# --- Alert management ---

class AlertConfig(BaseModel):
    urls: list[str]


@router.get("/alerts")
async def get_alerts():
    """Get current alert configuration (from DB)."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        from dockllama.alerts import load_alert_urls
        urls = load_alert_urls(conn)
        return {"urls": urls}
    finally:
        conn.close()


@router.put("/alerts")
async def update_alerts(alert_cfg: AlertConfig):
    """Update alert URLs (persisted to database)."""
    cfg = _get_cfg()
    cfg.alerts.urls = alert_cfg.urls
    from dockllama.alerts import init_alerts, save_alert_urls
    init_alerts(alert_cfg.urls)
    conn = init_db(cfg.monitoring.db_path)
    try:
        save_alert_urls(conn, alert_cfg.urls)
    finally:
        conn.close()
    return {"status": "ok", "count": len(alert_cfg.urls), "persisted": True}


@router.post("/alerts/test")
async def test_alerts():
    """Send a test notification to all configured alert targets."""
    cfg = _get_cfg()
    if not cfg.alerts.urls:
        raise HTTPException(400, "No alert URLs configured")
    from dockllama.alerts import _send
    import apprise
    success = _send(
        "DockLlama Test Notification",
        "This is a test notification from DockLlama. If you see this, notifications are working!",
        apprise.NotifyType.INFO,
    )
    return {"success": success, "targets": len(cfg.alerts.urls)}


# --- Digest history ---

@router.get("/digests/latest")
async def get_latest_digest():
    """Get the most recent digest."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        row = conn.execute(
            "SELECT id, date, generated_at, overall_health, headline, digest_json, formatted_text "
            "FROM digests ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            raise HTTPException(404, "No digests generated yet")
        import json as _json
        return {
            "id": row[0], "date": row[1], "generated_at": row[2],
            "overall_health": row[3], "headline": row[4],
            "digest": _json.loads(row[5]), "formatted_text": row[6],
        }
    finally:
        conn.close()


@router.get("/digests/{date}")
async def get_digest_by_date(date: str):
    """Get digest for a specific date (YYYY-MM-DD)."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        row = conn.execute(
            "SELECT id, date, generated_at, overall_health, headline, digest_json, formatted_text "
            "FROM digests WHERE date = ? ORDER BY id DESC LIMIT 1",
            (date,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"No digest for {date}")
        import json as _json
        return {
            "id": row[0], "date": row[1], "generated_at": row[2],
            "overall_health": row[3], "headline": row[4],
            "digest": _json.loads(row[5]), "formatted_text": row[6],
        }
    finally:
        conn.close()


@router.get("/digests")
async def list_digests(limit: int = Query(30, ge=1, le=365)):
    """List available digest dates."""
    cfg = _get_cfg()
    conn = init_db(cfg.monitoring.db_path)
    try:
        rows = conn.execute(
            "SELECT id, date, overall_health, headline FROM digests ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"id": r[0], "date": r[1], "overall_health": r[2], "headline": r[3]} for r in rows]
    finally:
        conn.close()

