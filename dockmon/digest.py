"""Daily digest — summarises the last 24 h of container health."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from dockmon.config import DockmonConfig
from dockmon.alerts import _send  # reuse the Apprise sender

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "v1_digest.txt"
PROMPT_VERSION = "v1_digest"


def _query_period_stats(conn: sqlite3.Connection, hours: int = 24) -> dict[str, Any]:
    """Pull per-container stats for the last *hours* from the events table."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """
        SELECT container,
               COUNT(*)                                          AS total,
               SUM(CASE WHEN ai_status = 'healthy'   THEN 1 ELSE 0 END) AS healthy,
               SUM(CASE WHEN ai_status = 'unhealthy' THEN 1 ELSE 0 END) AS unhealthy,
               SUM(CASE WHEN event_type = 'action' AND action_taken IN ('restart','dry_run_restart')
                    THEN 1 ELSE 0 END)                           AS restarts
        FROM events
        WHERE timestamp >= ? AND event_type = 'evaluation'
        GROUP BY container
        """,
        (cutoff,),
    ).fetchall()

    containers: list[dict] = []
    for name, total, healthy, unhealthy, restarts in rows:
        # Top root causes
        causes = conn.execute(
            """SELECT root_cause_category, COUNT(*) AS cnt
               FROM events
               WHERE container = ? AND timestamp >= ? AND ai_status = 'unhealthy'
                     AND root_cause_category IS NOT NULL AND root_cause_category != 'none'
               GROUP BY root_cause_category ORDER BY cnt DESC LIMIT 3""",
            (name, cutoff),
        ).fetchall()

        # Notable summaries (unhealthy ones, most recent first)
        summaries = conn.execute(
            """SELECT summary FROM events
               WHERE container = ? AND timestamp >= ? AND ai_status = 'unhealthy'
               ORDER BY timestamp DESC LIMIT 3""",
            (name, cutoff),
        ).fetchall()

        # Last evaluation
        last = conn.execute(
            """SELECT ai_status, confidence FROM events
               WHERE container = ? AND event_type = 'evaluation'
               ORDER BY timestamp DESC LIMIT 1""",
            (name,),
        ).fetchone()

        # Restart count from action events
        restart_count = conn.execute(
            """SELECT COUNT(*) FROM events
               WHERE container = ? AND timestamp >= ?
                     AND event_type = 'action'
                     AND action_taken IN ('restart', 'dry_run_restart')""",
            (name, cutoff),
        ).fetchone()[0]

        containers.append({
            "name": name,
            "total_evaluations": total,
            "healthy_count": healthy,
            "unhealthy_count": unhealthy,
            "restarts": restart_count,
            "top_root_causes": [c[0] for c in causes],
            "last_status": last[0] if last else "unknown",
            "last_confidence": last[1] if last else 0,
            "notable_summaries": [s[0] for s in summaries],
        })

    return {
        "period": f"Last {hours} hours",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "containers": containers,
    }



def _extract_json(text: str) -> dict:
    """Extract first JSON object from text, tolerating thinking tokens or preamble."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find first { and last } — extract the JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    logger.warning("Could not parse LLM digest response: %s", text[:200])
    return {"overall_health": "unknown", "headline": "Digest LLM returned unparseable response."}

async def generate_digest(cfg: DockmonConfig, conn: sqlite3.Connection) -> dict[str, Any]:
    """Generate a daily digest via the configured LLM."""
    stats = _query_period_stats(conn)

    # Add trend data for richer digest
    try:
        from dockmon.trends import get_fleet_trends
        container_names = [c["name"] for c in stats.get("containers", [])]
        if container_names:
            trends = get_fleet_trends(conn, container_names)
            stats["trends_7d_30d"] = {
                "fleet_health_pct_7d": trends["fleet_health_pct_7d"],
                "fleet_restarts_7d": trends["fleet_restarts_7d"],
                "worsening_containers": trends["worsening_containers"],
                "per_container": [
                    {"name": c["container"], "trend": c["trend"],
                     "health_7d": c["last_7d"]["health_pct"],
                     "health_30d": c["last_30d"]["health_pct"]}
                    for c in trends["containers"]
                ],
            }
    except Exception:
        logger.debug("Could not add trend data to digest", exc_info=True)


    if not stats["containers"]:
        logger.info("No evaluation data for digest period — skipping.")
        return {"overall_health": "unknown", "headline": "No monitoring data in the last 24 hours."}

    system_prompt = PROMPT_PATH.read_text() if PROMPT_PATH.exists() else "Summarise container health."
    model = cfg.ollama.digest_model or cfg.ollama.default_model

    payload = {
        "model": model,
        "prompt": json.dumps(stats, indent=2),
        "system": system_prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2, "num_predict": 4096, "think": False},
    }

    try:
        async with httpx.AsyncClient(timeout=cfg.ollama.timeout_seconds) as client:
            resp = await client.post(f"{cfg.ollama.base_url}/api/generate", json=payload)
            resp.raise_for_status()
            raw = resp.json().get("response", "{}")
            # gemma4 sometimes wraps JSON in thinking tokens — extract first JSON object
            digest = _extract_json(raw)
    except Exception:
        logger.exception("Digest generation failed")
        digest = {
            "overall_health": "unknown",
            "headline": "Digest generation failed — check Ollama connectivity.",
        }

    digest["_stats_input"] = stats
    return digest


def format_digest_text(digest: dict) -> str:
    """Format the digest dict into a human-readable text block."""
    lines = [
        f"=== Dockmon Daily Digest ===",
        f"Overall: {digest.get('overall_health', 'unknown').upper()}",
        f"{digest.get('headline', '')}",
        "",
    ]

    for cs in digest.get("container_summaries", []):
        lines.append(f"  [{cs.get('status_emoji', '?')}] {cs['name']}: {cs.get('one_liner', '')}")

    trends = digest.get("trends", [])
    if trends:
        lines.append("")
        lines.append("Trends:")
        for t in trends:
            lines.append(f"  - {t}")

    recs = digest.get("recommendations", [])
    if recs:
        lines.append("")
        lines.append("Recommendations:")
        for r in recs:
            lines.append(f"  - {r}")

    stats = digest.get("stats", {})
    if stats:
        lines.append("")
        lines.append(
            f"Stats: {stats.get('total_evaluations', 0)} evals, "
            f"{stats.get('total_unhealthy', 0)} unhealthy, "
            f"{stats.get('total_restarts', 0)} restarts"
        )

    return "\n".join(lines)


async def send_digest(cfg: DockmonConfig, conn: sqlite3.Connection) -> dict:
    """Generate, store, and send the daily digest."""
    digest = await generate_digest(cfg, conn)
    text = format_digest_text(digest)

    logger.info("Digest generated:\n%s", text)

    # Store in DB
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        conn.execute(
            """INSERT INTO digests (date, overall_health, headline, digest_json, formatted_text)
               VALUES (?, ?, ?, ?, ?)""",
            (today, digest.get("overall_health", "unknown"),
             digest.get("headline", ""), json.dumps(digest), text),
        )
        conn.commit()
        logger.info("Digest stored in DB for %s", today)
    except Exception:
        logger.exception("Failed to store digest in DB")

    if cfg.alerts.urls:
        _send("Dockmon Daily Digest", text)

    return digest
