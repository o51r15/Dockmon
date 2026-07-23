"""Trend engine — 7-day and 30-day health statistics with comparison."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _window_stats(conn: sqlite3.Connection, container: str, days: int) -> dict[str, Any]:
    """Calculate stats for a container over the last N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    row = conn.execute(
        """SELECT
               COUNT(*)                                                    AS total,
               SUM(CASE WHEN ai_status = 'healthy'   THEN 1 ELSE 0 END)   AS healthy,
               SUM(CASE WHEN ai_status = 'degraded'  THEN 1 ELSE 0 END)   AS degraded,
               SUM(CASE WHEN ai_status = 'unhealthy' THEN 1 ELSE 0 END)   AS unhealthy,
               SUM(CASE WHEN ai_status = 'critical'  THEN 1 ELSE 0 END)   AS critical,
               AVG(confidence)                                             AS avg_confidence
           FROM events
           WHERE container = ? AND timestamp >= ? AND event_type = 'evaluation'""",
        (container, cutoff),
    ).fetchone()

    total, healthy, degraded, unhealthy, critical, avg_conf = row
    total = total or 0
    healthy = healthy or 0
    degraded = degraded or 0
    unhealthy = unhealthy or 0
    critical = critical or 0

    restarts = conn.execute(
        """SELECT COUNT(*) FROM events
           WHERE container = ? AND timestamp >= ?
                 AND event_type = 'action'
                 AND action_taken IN ('restart', 'dry_run_restart')""",
        (container, cutoff),
    ).fetchone()[0]

    # Top root causes
    causes = conn.execute(
        """SELECT root_cause_category, COUNT(*) AS cnt
           FROM events
           WHERE container = ? AND timestamp >= ?
                 AND ai_status IN ('degraded', 'unhealthy', 'critical')
                 AND root_cause_category IS NOT NULL
                 AND root_cause_category != 'none'
           GROUP BY root_cause_category ORDER BY cnt DESC LIMIT 3""",
        (container, cutoff),
    ).fetchall()

    # Health % weights: healthy=1.0, degraded=0.5, unhealthy/critical=0
    health_pct = round(((healthy + degraded * 0.5) / total) * 100, 1) if total else 100.0

    return {
        "days": days,
        "total_evaluations": total,
        "healthy": healthy,
        "degraded": degraded,
        "unhealthy": unhealthy,
        "critical": critical,
        "restarts": restarts,
        "health_pct": health_pct,
        "avg_confidence": round(avg_conf, 1) if avg_conf else 0,
        "top_causes": [{"category": c[0], "count": c[1]} for c in causes],
    }


def _compare_windows(recent: dict, older: dict) -> str:
    """Compare two stat windows and return a trend direction."""
    if recent["total_evaluations"] == 0 or older["total_evaluations"] == 0:
        return "insufficient_data"

    diff = recent["health_pct"] - older["health_pct"]
    if diff > 5:
        return "improving"
    elif diff < -5:
        return "worsening"
    return "stable"


def get_container_trends(conn: sqlite3.Connection, container: str) -> dict[str, Any]:
    """Get 7d and 30d stats for a single container with trend comparison."""
    stats_7d = _window_stats(conn, container, 7)
    stats_30d = _window_stats(conn, container, 30)

    # Compare last 7d vs the 7d before that
    # We approximate "previous 7d" by using 14d stats minus 7d stats
    stats_14d = _window_stats(conn, container, 14)
    prev_7d = {
        "total_evaluations": stats_14d["total_evaluations"] - stats_7d["total_evaluations"],
        "healthy": stats_14d["healthy"] - stats_7d["healthy"],
        "degraded": stats_14d["degraded"] - stats_7d["degraded"],
        "unhealthy": stats_14d["unhealthy"] - stats_7d["unhealthy"],
        "critical": stats_14d.get("critical", 0) - stats_7d.get("critical", 0),
        "restarts": stats_14d["restarts"] - stats_7d["restarts"],
        "health_pct": 100.0,
        "avg_confidence": stats_14d["avg_confidence"],
        "top_causes": [],
        "days": 7,
    }
    if prev_7d["total_evaluations"] > 0:
        prev_7d["health_pct"] = round(
            ((prev_7d["healthy"] + prev_7d["degraded"] * 0.5) / prev_7d["total_evaluations"]) * 100, 1
        )

    trend = _compare_windows(stats_7d, prev_7d)

    return {
        "container": container,
        "trend": trend,
        "last_7d": stats_7d,
        "last_30d": stats_30d,
        "previous_7d_health_pct": prev_7d["health_pct"],
    }


def get_fleet_trends(conn: sqlite3.Connection, containers: list[str]) -> dict[str, Any]:
    """Get trends for all monitored containers."""
    container_trends = []
    fleet_healthy_7d = 0
    fleet_total_7d = 0
    fleet_restarts_7d = 0
    worsening = []

    for name in containers:
        t = get_container_trends(conn, name)
        container_trends.append(t)
        fleet_healthy_7d += t["last_7d"]["healthy"]
        fleet_total_7d += t["last_7d"]["total_evaluations"]
        fleet_restarts_7d += t["last_7d"]["restarts"]
        if t["trend"] == "worsening":
            worsening.append(name)

    fleet_health_pct = round((fleet_healthy_7d / fleet_total_7d) * 100, 1) if fleet_total_7d else 100.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fleet_health_pct_7d": fleet_health_pct,
        "fleet_total_evaluations_7d": fleet_total_7d,
        "fleet_restarts_7d": fleet_restarts_7d,
        "worsening_containers": worsening,
        "containers": container_trends,
    }
