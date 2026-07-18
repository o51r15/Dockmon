# Dockmon

AI-driven Docker container monitoring with automatic diagnostics and self-healing.

> **Alpha** — functional but under active development. All core features work; expect rough edges.

## Features

- **AI Log Evaluation** — streams container logs through a local LLM (Ollama) to detect failures and classify root causes
- **Smart Restart** — optionally restarts unhealthy containers with exponential backoff cooldowns and boot-loop protection
- **Dry-Run Mode** — evaluate and recommend without taking action (default)
- **Log Pre-Filtering** — drops ~90% of noise (INFO/DEBUG) before sending to the LLM
- **Web Dashboard** — real-time container status grid with SSE live updates
- **Log Explorer** — browse raw/filtered logs, trigger on-demand AI evaluations, view restart history
- **Daily Digest** — LLM-generated summary of the last 24h with trends and recommendations
- **Notifications** — alerts via Discord, Gotify, Telegram, email, and 80+ services (Apprise)
- **SQLite Storage** — WAL-mode database for events, cooldown state, and baselines

## Quick Start

```bash
git clone https://github.com/o51r15/Dockmon.git
cd Dockmon

cp config.example.yaml config.yaml
# Edit config.yaml: set your Ollama URL, model, and containers to monitor

# Docker
docker compose up -d

# Or run directly (Python 3.12+)
pip install -r requirements.txt
python -m dockmon.main config.yaml
```

Dashboard: `http://localhost:8556`

## Configuration

See `config.example.yaml` for all options. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama.base_url` | `http://localhost:11434` | Ollama API endpoint |
| `ollama.default_model` | `gemma4:latest` | Model for log evaluation |
| `monitoring.poll_interval_seconds` | `60` | Seconds between monitor cycles |
| `monitoring.dry_run` | `true` | Evaluate only, no restarts |
| `cooldowns.initial_minutes` | `5` | Cooldown after first restart |
| `cooldowns.max_restarts_per_hour` | `3` | Boot-loop breaker threshold |
| `digest.schedule_cron` | `0 7 * * *` | Daily digest time (UTC) |

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Docker, Ollama, DB connectivity |
| `/api/containers` | GET | Container list with latest evaluations |
| `/api/containers/{name}/logs` | GET | Filtered logs with stats |
| `/api/containers/{name}/evaluate` | POST | On-demand AI evaluation |
| `/api/events` | GET | Paginated event history |
| `/api/digest` | POST | Trigger daily digest on demand |
| `/api/stream` | GET | SSE stream for live updates |
| `/api/config` | GET | Running configuration |

## Architecture

```
Container Logs -> Pre-Filter (ANSI strip, level detect, ignore patterns)
              -> AI Engine (Ollama with structured JSON output)
              -> Action Engine (cooldown check -> restart or dry-run)
              -> Alerts (Apprise) + SSE (dashboard)
              -> SQLite (events, cooldowns, baselines)
```

## Requirements

- Docker host with `/var/run/docker.sock` accessible
- Ollama instance with a model loaded
- Python 3.12+ (if running outside Docker)

## License

MIT
