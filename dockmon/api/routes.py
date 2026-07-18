"""FastAPI routes for the Dockmon web interface."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from dockmon.config import DockmonConfig
from dockmon.db import init_db
from dockmon.docker_client import get_client, get_logs, list_containers
from dockmon.log_pipeline import process_logs
from dockmon.ai_engine import evaluate, EvaluationContext

router = APIRouter(prefix="/api")

# Set by main.py at startup
_cfg: DockmonConfig | None = None


def set_config(cfg: DockmonConfig) -> None:
    global _cfg
    _cfg = cfg


def _get_cfg() -> DockmonConfig:
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
                      action_taken, timestamp, model_used
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
