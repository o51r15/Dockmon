<div align="center">

# dockLlama

**AI-powered Docker container health monitoring with local LLMs.**

Watches your containers, analyzes logs with Ollama, and tells you what's wrong — no cloud, no API keys, no log shipping.

[![GitHub last commit](https://img.shields.io/github/last-commit/o51r15/DockLlama)](https://github.com/o51r15/DockLlama)
[![Docker Image](https://img.shields.io/badge/ghcr.io-dockllama%3Adev-blue)](https://ghcr.io/o51r15/dockllama)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)

</div>

---

> **Alpha** — functional and monitoring 18+ containers in production, but under active development. Expect rough edges.

## What It Does

dockLlama runs on a loop: it pulls logs from every container you configure, preprocesses them to strip noise, sends a structured summary to a local LLM (via Ollama), and records the health verdict. If something looks wrong, it can alert you or (optionally) restart the container automatically.

Unlike log aggregators (Loki, ELK) or uptime monitors (Uptime Kuma), dockLlama doesn't just detect *that* something failed — it uses an LLM to explain *what* failed and *why*, with root cause classification and recommended actions.

## Features

- **AI Log Evaluation** — each container's logs are analyzed by a local LLM with structured JSON output (status, health score, confidence, root cause, recommended action)
- **Structured Preprocessor** — Python-based log analyzer handles severity counting, error deduplication, recovery detection, and timestamp parsing *before* the LLM sees anything. Even 3B models get accurate results.
- **Per-Container Tuning** — custom system prompts, few-shot examples, and known-pattern tags teach the AI what's normal for each container
- **Resource Monitoring** — CPU and memory stats with sparklines on the dashboard and historical charts per container
- **Smart Restart** — optional automatic restarts with exponential backoff, dependency-aware group ordering, and boot-loop protection
- **Container Management** — add/remove monitored containers from the UI with config archival and optional data purge
- **Model Selection** — discover, benchmark, and switch Ollama models from the settings UI with hardware-aware interval recommendations
- **Daily Digest** — LLM-generated fleet summary with 24h trends and recommendations
- **Notifications** — Discord, Gotify, Telegram, email, and 80+ services via Apprise
- **Web Dashboard** — real-time status grid with SSE live updates, sparklines, and detail drawers
- **Log Explorer** — browse raw/filtered logs, trigger on-demand evaluations, view event history
- **Prompt Editor** — customize AI instructions per container with live testing against real logs
- **Dry-Run Mode** — evaluate and recommend without taking action (default)
- **SQLite Storage** — WAL-mode database for events, stats, baselines, cooldowns, and model test results

## Quick Start

### Docker (recommended)

```bash
# 1. Create your config
mkdir -p ~/dockllama
curl -o ~/dockllama/config.yaml \
  https://raw.githubusercontent.com/o51r15/DockLlama/main/config.example.yaml

# 2. Edit config.yaml — set your Ollama URL and containers to monitor
nano ~/dockllama/config.yaml

# 3. Run
docker run -d \
  --name dockllama \
  --restart unless-stopped \
  -p 8556:8556 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v ~/dockllama/config.yaml:/app/config/config.yaml \
  -v dockllama-data:/app/data \
  -e TZ=America/New_York \
  ghcr.io/o51r15/dockllama:dev
```

Dashboard: **http://localhost:8556**

### From Source

```bash
git clone https://github.com/o51r15/DockLlama.git
cd DockLlama
cp config.example.yaml config.yaml
# Edit config.yaml

pip install -r requirements.txt
python -m dockllama config.yaml
```

Requires Python 3.12+ and an accessible Ollama instance with at least one model pulled.

## Configuration

See [`config.example.yaml`](config.example.yaml) for all options. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama.base_url` | `http://localhost:11434` | Ollama API endpoint |
| `ollama.default_model` | `llama3.1:8b` | Model for log evaluation |
| `ollama.digest_model` | `llama3:8b` | Model for daily digest summaries |
| `monitoring.poll_interval_seconds` | `60` | Seconds between monitor cycles |
| `monitoring.dry_run` | `true` | Evaluate only, no restarts |
| `cooldowns.initial_minutes` | `5` | Cooldown after first restart |
| `cooldowns.max_restarts_per_hour` | `3` | Boot-loop breaker threshold |
| `digest.schedule_cron` | `0 7 * * *` | Daily digest time (UTC) |

### Per-Container Config

```yaml
containers:
  - name: postgres
    enabled: true
    context_prompt: "This is a PostgreSQL database. Planned restarts with FATAL shutdown messages are normal maintenance."
    ignore_patterns:
      - "checkpoint starting"
      - "redo done at"
    model_override: "qwen2.5:14b"    # use a bigger model for critical containers
    compose_group: "database-stack"   # restart group ordering
```

## How It Works

```
Container Logs
  → Log Analyzer (severity count, error dedup, recovery detection)
  → Structured Summary (~20 lines instead of ~200 raw)
  → Ollama LLM (structured JSON: status, score, confidence, root cause, action)
  → Action Engine (cooldown check → restart or dry-run log)
  → Alerts (Apprise) + SSE (dashboard) + SQLite (events)
```

The preprocessor does all mechanical analysis in Python — the LLM only interprets patterns. This means model size matters far less than with raw log parsing, and even 3B models get correct results on clear-cut cases.

## Model Recommendations

| Model | Size | Eval Time | Notes |
|-------|------|-----------|-------|
| `llama3.1:8b` | 4.7 GB | ~11s | **Recommended.** Best speed/quality balance |
| `llama3.2:3b` | 2.0 GB | ~3-5s | Fast, good for large fleets or constrained hardware |
| `qwen2.5:14b` | 9.0 GB | ~15s | Maximum accuracy on ambiguous edge cases |
| `gemma3:4b` | 3.3 GB | ~6s | Fast alternative with decent quality |

Models can be benchmarked and switched from **Settings > Models** in the UI, with hardware-aware interval calculations.

## API

All endpoints are under `/api/`. Key routes:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/containers` | GET | Container list with latest evaluations |
| `/api/containers/{name}/evaluate` | POST | On-demand AI evaluation |
| `/api/containers/{name}/logs` | GET | Filtered logs with stats |
| `/api/containers/{name}/stats` | GET | CPU/memory history (1h/24h/7d/30d) |
| `/api/containers/{name}/prompt` | GET/PUT/DELETE | Per-container prompt config |
| `/api/containers/{name}/test-prompt` | POST | Test prompt against live logs |
| `/api/docker/containers` | GET | All Docker containers (for add/remove UI) |
| `/api/containers/add` | POST | Add container to monitoring |
| `/api/containers/{name}` | DELETE | Remove container (with optional `?purge=true`) |
| `/api/events` | GET | Paginated event history |
| `/api/digest` | POST | Trigger daily digest |
| `/api/models` | GET | Available Ollama models |
| `/api/models/test` | POST | Benchmark a model |
| `/api/models/interval-calc` | GET | Hardware-aware interval calculator |
| `/api/stats/fleet` | GET | Fleet-wide resource overview |
| `/api/health` | GET | Docker, Ollama, DB connectivity |
| `/api/stream` | GET | SSE stream for live dashboard updates |
| `/api/config` | GET | Running configuration |

## Requirements

- Docker host with `/var/run/docker.sock` accessible
- [Ollama](https://ollama.com) instance with at least one model pulled (can be on a different machine)
- Python 3.12+ (if running outside Docker)

## Tech Stack

Python 3.12+, FastAPI, Alpine.js, Chart.js, SQLite (WAL mode), Ollama, Apprise, httpx

## License

[MIT](LICENSE)
