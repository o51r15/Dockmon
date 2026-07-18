"""AI evaluation engine — sends filtered logs to Ollama and parses structured responses."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from pydantic import BaseModel, field_validator

from dockmon.config import OllamaConfig

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"
DEFAULT_PROMPT_VERSION = "v5_evaluate"


class EvaluationResult(BaseModel):
    """Structured response from the LLM."""
    status: str  # "healthy", "degraded", "unhealthy", or "critical"
    health_score: int  # 0-100 numeric health score
    confidence: int
    root_cause_category: str
    error_origin: str  # "internal", "external", or "none"
    summary: str
    restart_would_help: bool
    restart_reasoning: str
    recommended_action: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        v = v.lower().strip()
        if v not in ("healthy", "degraded", "unhealthy", "critical"):
            raise ValueError(f"Invalid status: {v}")
        return v

    @field_validator("health_score")
    @classmethod
    def validate_health_score(cls, v: int) -> int:
        return max(0, min(100, v))

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: int) -> int:
        return max(0, min(100, v))

    @field_validator("root_cause_category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        v = v.lower().strip()
        valid = {"oom", "network", "config", "dependency", "crash", "storage", "none"}
        return v if v in valid else "none"

    @field_validator("error_origin")
    @classmethod
    def validate_origin(cls, v: str) -> str:
        v = v.lower().strip()
        return v if v in ("internal", "external", "none") else "none"

    @field_validator("recommended_action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        v = v.lower().strip()
        return v if v in ("none", "restart") else "none"


@dataclass
class EvaluationContext:
    """Everything needed to evaluate a container's logs."""
    container_name: str
    filtered_lines: list[str]
    model: str
    structured_summary: Optional[str] = None  # from log_analyzer
    baseline_sample: Optional[str] = None
    prompt_version: str = DEFAULT_PROMPT_VERSION


def _load_prompt(version: str) -> str:
    """Load a versioned prompt template."""
    path = PROMPT_DIR / f"{version}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text().strip()


def _build_messages(ctx: EvaluationContext) -> tuple[str, str]:
    """Build system and user prompts for the LLM."""
    system_prompt = _load_prompt(ctx.prompt_version)

    # v5: use structured summary from log_analyzer if available
    if ctx.structured_summary:
        return system_prompt, ctx.structured_summary

    # Fallback: old-style filtered lines (v4 compat)
    user_parts = [f"Container: {ctx.container_name}"]

    if ctx.baseline_sample:
        user_parts.append(
            f"\nFor reference, here is what normal/healthy logs look like for this container:\n"
            f"---\n{ctx.baseline_sample}\n---"
        )

    user_parts.append(f"\nRecent log lines ({len(ctx.filtered_lines)} lines, pre-filtered to WARN/ERROR/unknown only):")
    user_parts.append("Line 1 is the OLDEST, line {0} is the MOST RECENT. Weight recent lines more heavily.".format(len(ctx.filtered_lines)))
    user_parts.append("---")
    numbered = [f"[{i+1}/{len(ctx.filtered_lines)}] {line}" for i, line in enumerate(ctx.filtered_lines)]
    user_parts.append("\n".join(numbered))
    user_parts.append("---")

    return system_prompt, "\n".join(user_parts)


def _make_fallback(container_name: str, reason: str) -> EvaluationResult:
    """Return a safe fallback result when the LLM fails."""
    return EvaluationResult(
        status="healthy",
        health_score=50,
        confidence=0,
        root_cause_category="none",
        error_origin="none",
        summary=f"Evaluation failed: {reason}. Failing open (assuming healthy).",
        restart_would_help=False,
        restart_reasoning="Evaluation failed, cannot determine.",
        recommended_action="none",
    )


async def evaluate(
    ctx: EvaluationContext,
    ollama_config: OllamaConfig,
) -> tuple[EvaluationResult, str]:
    """
    Send filtered logs to Ollama and parse the structured response.

    Returns (result, prompt_version).
    Fails open on any error — returns a healthy fallback rather than crashing.
    """
    if not ctx.filtered_lines and not ctx.structured_summary:
        return EvaluationResult(
            status="healthy",
            health_score=95,
            confidence=95,
            root_cause_category="none",
            error_origin="none",
            summary="No warning or error lines detected in recent logs.",
            restart_would_help=False,
            restart_reasoning="No issues detected.",
            recommended_action="none",
        ), ctx.prompt_version

    system_prompt, user_prompt = _build_messages(ctx)

    try:
        async with httpx.AsyncClient(timeout=ollama_config.timeout_seconds) as client:
            response = await client.post(
                f"{ollama_config.base_url}/api/generate",
                json={
                    "model": ctx.model,
                    "system": system_prompt,
                    "prompt": user_prompt,
                    "format": "json",
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.1,
                        "num_predict": 500,
                    },
                },
            )
            response.raise_for_status()
    except httpx.TimeoutException:
        logger.warning("Ollama timeout for %s", ctx.container_name)
        return _make_fallback(ctx.container_name, "Ollama request timed out"), ctx.prompt_version
    except httpx.HTTPError as e:
        logger.warning("Ollama HTTP error for %s: %s", ctx.container_name, e)
        return _make_fallback(ctx.container_name, f"Ollama HTTP error: {e}"), ctx.prompt_version

    # Parse response
    try:
        data = response.json()
        raw_text = data.get("response", "")
        parsed = json.loads(raw_text)
        result = EvaluationResult(**parsed)
        return result, ctx.prompt_version
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(
            "Failed to parse Ollama response for %s: %s\nRaw: %s",
            ctx.container_name, e, raw_text[:500] if 'raw_text' in dir() else "no response",
        )
        # Retry once
        try:
            async with httpx.AsyncClient(timeout=ollama_config.timeout_seconds) as client:
                response = await client.post(
                    f"{ollama_config.base_url}/api/generate",
                    json={
                        "model": ctx.model,
                        "system": system_prompt,
                        "prompt": user_prompt + "\n\nIMPORTANT: Your previous response was not valid JSON. Respond with ONLY valid JSON.",
                        "format": "json",
                        "stream": False,
                        "think": False,
                        "options": {"temperature": 0.0, "num_predict": 300},
                    },
                )
                response.raise_for_status()
                data = response.json()
                raw_text = data.get("response", "")
                parsed = json.loads(raw_text)
                result = EvaluationResult(**parsed)
                return result, ctx.prompt_version
        except Exception:
            pass

        return _make_fallback(ctx.container_name, f"Invalid LLM response: {e}"), ctx.prompt_version


if __name__ == "__main__":
    import asyncio

    async def test():
        cfg = OllamaConfig(base_url="http://localhost:11434")
        ctx = EvaluationContext(
            container_name="test-container",
            filtered_lines=[
                "WARN\tprowlarr\tprowlarr/crawler.go:187\tprowlarr: search failed\t{\"error\": \"API returned status 400\"}",
                "ERROR\tapp\tserver.go:55\tfailed to connect to database\t{\"error\": \"connection refused\"}",
            ],
            model=cfg.default_model,
        )
        result, version = await evaluate(ctx, cfg)
        print(f"Status: {result.status}")
        print(f"Confidence: {result.confidence}")
        print(f"Category: {result.root_cause_category}")
        print(f"Summary: {result.summary}")
        print(f"Action: {result.recommended_action}")
        print(f"Prompt version: {version}")

    asyncio.run(test())
