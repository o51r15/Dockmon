"""Restart engine — translates AI recommendations into Docker actions with cooldown enforcement."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import docker
from docker.models.containers import Container

from dockllama.ai_engine import EvaluationResult
from dockllama.config import CooldownConfig

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Outcome of an action decision."""
    container_name: str
    action_taken: str        # "none", "restart", "dry_run_restart", "cooldown_skip", "alert_only"
    success: bool
    message: str
    log_snapshot: str = ""



def resolve_dependency_group(
    container_name: str,
    dependency_groups: dict[str, list[str]],
) -> tuple[str | None, list[str] | None]:
    """Check if a container belongs to a dependency group.
    
    Returns (group_name, ordered_member_list) or (None, None).
    The member list defines restart order — first member restarts first.
    """
    for group_name, members in dependency_groups.items():
        if container_name in members:
            return group_name, list(members)  # preserve config order
    return None, None

def _get_cooldown_state(conn: sqlite3.Connection, container: str) -> dict | None:
    """Fetch current cooldown state for a container."""
    row = conn.execute(
        "SELECT last_restart, consecutive_restarts, current_cooldown_minutes, alert_only_mode "
        "FROM cooldowns WHERE container = ?",
        (container,),
    ).fetchone()
    if row:
        return {
            "last_restart": row[0],
            "consecutive_restarts": row[1],
            "current_cooldown_minutes": row[2],
            "alert_only_mode": row[3],
        }
    return None


def _is_in_cooldown(state: dict | None) -> bool:
    """Check if a container is still within its cooldown window."""
    if state is None:
        return False
    last = datetime.fromisoformat(state["last_restart"])
    cooldown_end = last + timedelta(minutes=state["current_cooldown_minutes"])
    return datetime.now(timezone.utc) < cooldown_end


def _update_cooldown(
    conn: sqlite3.Connection,
    container: str,
    cooldown_cfg: CooldownConfig,
    state: dict | None,
) -> None:
    """Update cooldown state after a restart."""
    now = datetime.now(timezone.utc).isoformat()

    if state is None:
        consecutive = 1
        cooldown = cooldown_cfg.initial_minutes
    else:
        consecutive = state["consecutive_restarts"] + 1
        cooldown = min(
            cooldown_cfg.initial_minutes * (cooldown_cfg.backoff_multiplier ** (consecutive - 1)),
            cooldown_cfg.max_cooldown_minutes,
        )

    # Check boot-loop threshold
    alert_only = 1 if consecutive >= cooldown_cfg.max_restarts_per_hour else 0

    conn.execute(
        """INSERT INTO cooldowns (container, last_restart, consecutive_restarts,
           current_cooldown_minutes, alert_only_mode)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(container) DO UPDATE SET
             last_restart = excluded.last_restart,
             consecutive_restarts = excluded.consecutive_restarts,
             current_cooldown_minutes = excluded.current_cooldown_minutes,
             alert_only_mode = excluded.alert_only_mode""",
        (container, now, consecutive, cooldown, alert_only),
    )
    conn.commit()

    if alert_only:
        logger.warning(
            "[%s] BOOT-LOOP BREAKER: %d consecutive restarts — entering alert-only mode",
            container, consecutive,
        )
    else:
        logger.info(
            "[%s] Cooldown updated: %d consecutive, next cooldown %d min",
            container, consecutive, cooldown,
        )


def reset_cooldown_if_healthy(
    conn: sqlite3.Connection,
    container: str,
    healthy_threshold_minutes: int = 30,
) -> None:
    """Reset cooldown state if a container has been healthy long enough."""
    state = _get_cooldown_state(conn, container)
    if state is None:
        return
    if state["consecutive_restarts"] == 0 and state["alert_only_mode"] == 0:
        return

    last = datetime.fromisoformat(state["last_restart"])
    healthy_since = last + timedelta(minutes=healthy_threshold_minutes)
    if datetime.now(timezone.utc) >= healthy_since:
        conn.execute(
            "UPDATE cooldowns SET consecutive_restarts = 0, "
            "current_cooldown_minutes = 5, alert_only_mode = 0 "
            "WHERE container = ?",
            (container,),
        )
        conn.commit()
        logger.info("[%s] Cooldown reset — healthy for %d+ minutes", container, healthy_threshold_minutes)


def execute_action(
    result: EvaluationResult,
    container: Container,
    conn: sqlite3.Connection,
    cooldown_cfg: CooldownConfig,
    dry_run: bool,
    log_snapshot: str = "",
    compose_group: str | None = None,
    group_container_names: list[str] | None = None,
    dependency_group_name: str | None = None,
    dependency_group_members: list[str] | None = None,
) -> ActionResult:
    """
    Decide and execute an action based on the AI evaluation.

    Returns an ActionResult describing what happened.
    """
    name = container.name

    # No action recommended
    if result.recommended_action != "restart":
        # If healthy, check if we should reset cooldowns
        if result.status in ("healthy", "degraded") and result.confidence >= 70:
            reset_cooldown_if_healthy(conn, name)
        return ActionResult(
            container_name=name,
            action_taken="none",
            success=True,
            message="No action needed.",
        )

    # Restart recommended — check cooldown state
    state = _get_cooldown_state(conn, name)

    # Alert-only mode (boot-loop breaker)
    if state and state["alert_only_mode"]:
        return ActionResult(
            container_name=name,
            action_taken="alert_only",
            success=True,
            message=f"Container in alert-only mode after {state['consecutive_restarts']} "
                    f"consecutive restarts. Manual intervention required.",
            log_snapshot=log_snapshot,
        )

    # In cooldown
    if _is_in_cooldown(state):
        remaining = (
            datetime.fromisoformat(state["last_restart"])
            + timedelta(minutes=state["current_cooldown_minutes"])
            - datetime.now(timezone.utc)
        )
        return ActionResult(
            container_name=name,
            action_taken="cooldown_skip",
            success=True,
            message=f"Restart skipped — cooldown active ({int(remaining.total_seconds())}s remaining).",
            log_snapshot=log_snapshot,
        )

    # Dry-run mode
    if dry_run:
        group_msg = ""
        if dependency_group_name and dependency_group_members:
            group_msg = f" [dependency group '{dependency_group_name}': would restart in order: {' → '.join(dependency_group_members)}]"
        elif compose_group and group_container_names:
            group_msg = f" [compose group '{compose_group}': would restart {', '.join(group_container_names)}]"
        logger.info("[%s] DRY RUN: would restart (confidence=%d, reason=%s)%s",
                     name, result.confidence, result.root_cause_category, group_msg)
        return ActionResult(
            container_name=name,
            action_taken="dry_run_restart",
            success=True,
            message=f"DRY RUN — would restart. Reason: {result.summary}{group_msg}",
            log_snapshot=log_snapshot,
        )

    # Execute restart — dependency group (ordered), compose group, or single container
    if dependency_group_name and dependency_group_members and len(dependency_group_members) > 1:
        logger.info("[%s] Restarting dependency group '%s' in order: %s",
                     name, dependency_group_name, " → ".join(dependency_group_members))
    elif compose_group and group_container_names and len(group_container_names) > 1:
        logger.info("[%s] Restarting compose group '%s': %s",
                     name, compose_group, ", ".join(group_container_names))
    else:
        logger.info("[%s] Restarting container...", name)

    # Store pre-restart snapshot
    conn.execute(
        """INSERT INTO events (container, event_type, ai_status, confidence,
           root_cause_category, summary, action_taken, log_snapshot, model_used)
           VALUES (?, 'restart', ?, ?, ?, ?, 'restart', ?, 'n/a')""",
        (name, result.status, result.confidence, result.root_cause_category,
         result.summary, log_snapshot),
    )
    conn.commit()

    try:
        # Determine which containers to restart (dependency group takes precedence)
        containers_to_restart = [container]
        restart_order_names = None
        if dependency_group_name and dependency_group_members and len(dependency_group_members) > 1:
            client = docker.from_env()
            all_running = {c.name: c for c in client.containers.list()}
            # Use dependency group order (defined in config)
            containers_to_restart = [
                all_running[n] for n in dependency_group_members if n in all_running
            ]
            restart_order_names = dependency_group_members
        elif compose_group and group_container_names and len(group_container_names) > 1:
            client = docker.from_env()
            all_running = {c.name: c for c in client.containers.list()}
            containers_to_restart = [
                all_running[n] for n in group_container_names if n in all_running
            ]

        for c in containers_to_restart:
            logger.info("[%s] Restarting %s...", name, c.name)
            c.restart(timeout=30)

        # Verify: check at 10s and 30s
        time.sleep(10)
        all_ok = True
        for c in containers_to_restart:
            c.reload()
            if c.status != "running":
                time.sleep(20)
                c.reload()
            if c.status != "running":
                all_ok = False
                logger.warning("[%s] %s status is '%s' after restart", name, c.name, c.status)

        # Log cooldown for each container in the group to prevent cascading restarts
        restarted_names = [c.name for c in containers_to_restart]
        for rname in restarted_names:
            rstate = _get_cooldown_state(conn, rname)
            _update_cooldown(conn, rname, cooldown_cfg, rstate)
        if dependency_group_name:
            group_note = f" (dependency group '{dependency_group_name}': {' → '.join(restarted_names)})"
        elif len(restarted_names) > 1:
            group_note = f" (compose group: {', '.join(restarted_names)})"
        else:
            group_note = ""

        if all_ok:
            return ActionResult(
                container_name=name,
                action_taken="restart",
                success=True,
                message=f"Restarted successfully{group_note}.",
                log_snapshot=log_snapshot,
            )
        else:
            return ActionResult(
                container_name=name,
                action_taken="restart",
                success=False,
                message=f"Restart issued but not all containers healthy after 30s{group_note}.",
                log_snapshot=log_snapshot,
            )

    except Exception as e:
        logger.exception("[%s] Restart failed", name)
        return ActionResult(
            container_name=name,
            action_taken="restart",
            success=False,
            message=f"Restart failed: {e}",
            log_snapshot=log_snapshot,
        )
