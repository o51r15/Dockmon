"""Notification layer — sends alerts via Apprise to Discord, Gotify, Telegram, etc."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import apprise

from dockmon.actions import ActionResult
from dockmon.ai_engine import EvaluationResult

logger = logging.getLogger(__name__)

# Reusable Apprise instance
_apprise: apprise.Apprise | None = None


def init_alerts(urls: list[str]) -> None:
    """Initialize the Apprise notification client."""
    global _apprise
    _apprise = apprise.Apprise()
    for url in urls:
        _apprise.add(url)
    if urls:
        logger.info("Alerts configured: %d notification target(s)", len(urls))
    else:
        logger.info("No alert URLs configured — notifications disabled")


def _send(title: str, body: str, notify_type: apprise.NotifyType = apprise.NotifyType.WARNING) -> bool:
    """Send a notification. Returns True on success."""
    if _apprise is None or len(_apprise) == 0:
        logger.debug("Alert skipped (no targets): %s", title)
        return False

    try:
        result = _apprise.notify(title=title, body=body, notify_type=notify_type)
        if result:
            logger.info("Alert sent: %s", title)
        else:
            logger.warning("Alert delivery failed: %s", title)
        return result
    except Exception:
        logger.exception("Alert error: %s", title)
        return False


def alert_restart(container_name: str, result: EvaluationResult, action: ActionResult) -> bool:
    """Send an alert when a container is restarted."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"🔄 Dockmon: Restarted {container_name}"
    body = (
        f"**Container:** {container_name}\n"
        f"**Time:** {now}\n"
        f"**Status:** {result.status} (confidence: {result.confidence}%)\n"
        f"**Root cause:** {result.root_cause_category}\n"
        f"**Summary:** {result.summary}\n"
        f"**Action:** {action.action_taken}\n"
        f"**Result:** {action.message}"
    )
    return _send(title, body, apprise.NotifyType.WARNING)


def alert_dry_run(container_name: str, result: EvaluationResult) -> bool:
    """Send an alert for a dry-run restart recommendation."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"⚠️ Dockmon: Would restart {container_name} (dry run)"
    body = (
        f"**Container:** {container_name}\n"
        f"**Time:** {now}\n"
        f"**Status:** {result.status} (confidence: {result.confidence}%)\n"
        f"**Root cause:** {result.root_cause_category}\n"
        f"**Summary:** {result.summary}\n"
        f"**Action:** Restart recommended but dry_run=true"
    )
    return _send(title, body, apprise.NotifyType.INFO)


def alert_escalation(container_name: str, consecutive_restarts: int) -> bool:
    """Send an alert when a container enters alert-only mode (boot-loop breaker)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = f"🚨 Dockmon: {container_name} in alert-only mode"
    body = (
        f"**Container:** {container_name}\n"
        f"**Time:** {now}\n"
        f"**Consecutive restarts:** {consecutive_restarts}\n"
        f"**Status:** Automatic restarts DISABLED.\n"
        f"**Action required:** Manual intervention needed. "
        f"The container will continue to be monitored but Dockmon will not restart it "
        f"until it has been healthy for 30+ minutes."
    )
    return _send(title, body, apprise.NotifyType.FAILURE)


def alert_cooldown_skip(container_name: str, remaining_seconds: int) -> bool:
    """Send an alert when a restart is skipped due to cooldown."""
    title = f"⏳ Dockmon: {container_name} restart skipped (cooldown)"
    body = (
        f"**Container:** {container_name}\n"
        f"**Restart recommended** but cooldown is active.\n"
        f"**Time remaining:** {remaining_seconds}s"
    )
    return _send(title, body, apprise.NotifyType.INFO)


def alert_error(message: str) -> bool:
    """Send an alert for system-level errors (Ollama down, etc.)."""
    title = "❌ Dockmon: System Error"
    return _send(title, message, apprise.NotifyType.FAILURE)
