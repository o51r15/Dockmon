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
    stats_retention_days: int = 7


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
    _config_path: str = "/app/config/config.yaml"  # set after load
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

    cfg_obj = DockLlamaConfig(**raw)
    cfg_obj._config_path = str(p)
    return cfg_obj

import re as _re


def _update_yaml_field(config_path: str, field: str, value) -> None:
    """Update a single top-level or nested field in config.yaml using regex, preserving comments and formatting."""
    with open(config_path) as f:
        text = f.read()

    if isinstance(value, str):
        val_str = value
    elif isinstance(value, bool):
        val_str = str(value).lower()
    elif isinstance(value, (int, float)):
        val_str = str(value)
    else:
        val_str = str(value)

    # Match "  field: old_value" pattern (handles indented fields)
    pattern = _re.compile(r"^(\s*" + _re.escape(field) + r":\s*)(.+)$", _re.MULTILINE)
    new_text, count = pattern.subn(r"\g<1>" + val_str, text)

    if count == 0:
        raise ValueError(f"Field '{field}' not found in {config_path}")
    if count > 1:
        raise ValueError(f"Field '{field}' matched {count} times in {config_path} — ambiguous")

    with open(config_path, "w") as f:
        f.write(new_text)



def save_containers_to_config(cfg: "DockLlamaConfig") -> None:
    """Rewrite the containers list in config.yaml. Preserves all other sections."""
    p = Path(cfg._config_path)
    with open(p) as f:
        raw = yaml.safe_load(f) or {}

    # Serialize containers from the live config
    containers_data = []
    for c in cfg.containers:
        entry = {"name": c.name, "enabled": c.enabled}
        if c.ignore_patterns:
            entry["ignore_patterns"] = c.ignore_patterns
        if c.compose_group:
            entry["compose_group"] = c.compose_group
        if c.model_override:
            entry["model_override"] = c.model_override
        if c.context_prompt:
            entry["context_prompt"] = c.context_prompt
        if c.examples:
            entry["examples"] = [dict(e) for e in c.examples]
        if c.known_patterns:
            entry["known_patterns"] = [dict(kp) for kp in c.known_patterns]
        containers_data.append(entry)

    raw["containers"] = containers_data

    # Also sync dependency_groups
    if cfg.dependency_groups:
        raw["dependency_groups"] = dict(cfg.dependency_groups)

    with open(p, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def save_poll_interval(cfg: "DockLlamaConfig") -> None:
    """Persist the current poll_interval_seconds to config.yaml."""
    _update_yaml_field(cfg._config_path, "poll_interval_seconds", cfg.monitoring.poll_interval_seconds)


def save_default_model(cfg: "DockLlamaConfig", role: str = "eval") -> None:
    """Persist the current default model to config.yaml."""
    field = "default_model" if role == "eval" else "digest_model"
    value = cfg.ollama.default_model if role == "eval" else cfg.ollama.digest_model
    _update_yaml_field(cfg._config_path, field, value)


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.example.yaml"
    cfg = load_config(cfg_path)
    print(f"Config loaded OK: {len(cfg.containers)} containers configured")
    for c in cfg.containers:
        print(f"  - {c.name} (enabled={c.enabled})")
