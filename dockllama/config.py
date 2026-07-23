"""Configuration loader and validation for DockLlama."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, field_validator


class OllamaConfig(BaseModel):
    base_url: str = "http://ollama:11434"
    default_model: str = "gemma2:2b"
    digest_model: str = "llama3:8b"
    timeout_seconds: int = 30


class MonitoringConfig(BaseModel):
    poll_interval_seconds: int = 60
    log_lines_per_check: int = 200
    dry_run: bool = True
    db_path: str = "/app/data/dockllama.db"
    retention_days: int = 90


class ContainerConfig(BaseModel):
    name: str
    enabled: bool = True
    model_override: Optional[str] = None
    ignore_patterns: list[str] = []
    compose_group: Optional[str] = None
    context_prompt: Optional[str] = None
    examples: list[dict] = []
    known_patterns: list[dict] = []


class CooldownConfig(BaseModel):
    initial_minutes: int = 5
    backoff_multiplier: int = 3
    max_cooldown_minutes: int = 120
    max_restarts_per_hour: int = 3


class AlertConfig(BaseModel):
    urls: list[str] = []


class DigestConfig(BaseModel):
    enabled: bool = True
    schedule_cron: str = "0 7 * * *"


class DockLlamaConfig(BaseModel):
    ollama: OllamaConfig = OllamaConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    containers: list[ContainerConfig] = []
    cooldowns: CooldownConfig = CooldownConfig()
    alerts: AlertConfig = AlertConfig()
    digest: DigestConfig = DigestConfig()
    dependency_groups: dict[str, list[str]] = {}

    @field_validator("containers")
    @classmethod
    def at_least_one_container(cls, v: list[ContainerConfig]) -> list[ContainerConfig]:
        if not v:
            print("WARNING: No containers configured for monitoring.")
        return v


def load_config(path: str | Path = "/app/config/config.yaml") -> DockLlamaConfig:
    """Load and validate configuration from a YAML file."""
    p = Path(path)
    if not p.exists():
        print(f"Config file not found: {p}", file=sys.stderr)
        print("Copy config.example.yaml to config.yaml and edit it.", file=sys.stderr)
        sys.exit(1)

    with open(p) as f:
        raw = yaml.safe_load(f) or {}

    return DockLlamaConfig(**raw)


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.example.yaml"
    cfg = load_config(cfg_path)
    print(f"Config loaded OK: {len(cfg.containers)} containers configured")
    for c in cfg.containers:
        print(f"  - {c.name} (enabled={c.enabled})")
