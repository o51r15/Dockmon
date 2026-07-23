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
python -m dockllama config.yaml
```

Dashboard: `http://localhost:8556`

## Configuration

See `config.example.yaml` for all options. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama.base_url` | `http://localhost:11434` | Ollama API endpoint |
| `ollama.default_model` | `llama3.1:8b` | Model for log evaluation |
| `monitoring.poll_interval_seconds` | `60` | Seconds between monitor cycles |
| `monitoring.dry_run` | `true` | Evaluate only, no restarts |
| `cooldowns.initial_minutes` | `5` | Cooldown after first restart |
| `cooldowns.max_restarts_per_hour` | `3` | Boot-loop breaker threshold |
| `digest.schedule_cron` | `0 7 * * *` | Daily digest time (UTC) |

## Model Selection

Dockmon uses two models: one for container log evaluation (runs per container per cycle) and one for daily digest summaries. Both run locally via Ollama.

### Benchmark Results

All models were tested against the same structured log summary — a PostgreSQL container with FATAL errors during a planned shutdown followed by successful recovery. The previous raw-log pipeline incorrectly marked this container as UNHEALTHY; all models correctly identified it as HEALTHY when given the v5 structured preprocessor output.

| Model | Size | Eval Time (warm) | Accuracy | Best For |
|-------|------|-------------------|----------|----------|
| `llama3.2:3b` | 2.0 GB | ~3–5s | Good | Low-power hardware, fast scans, large container counts |
| `llama3.1:8b` | 4.7 GB | ~11s | Very good | **Recommended default** — best speed/quality balance |
| `qwen2.5:14b` | 9.0 GB | ~15s | Very good | Higher accuracy on ambiguous edge cases |
| `qwen3:14b` | 9.0 GB | ~15s | Very good | Similar to qwen2.5, requires `think: false` workaround |
| `gemma3:4b` | 3.3 GB | ~6s | Good | Fast alternative with decent quality |
| `gemma4:latest` | — | — | — | Used for digest summaries (not log eval) |

### Recommendations

**For most setups:** Use `llama3.1:8b`. It handles all common container log patterns correctly — PostgreSQL shutdown/restart cycles, transient connection errors, startup noise, and steady-state operation. At ~11 seconds per evaluation it's fast enough for 15+ containers on a 60-second poll interval.

**For resource-constrained systems:** Use `llama3.2:3b`. At 3–5 seconds per eval it's 2–3x faster and still gets straightforward healthy/unhealthy calls right. It may struggle with subtle degraded-vs-healthy distinctions in edge cases.

**For maximum accuracy:** Use `qwen2.5:14b`. The extra 4 seconds per eval buys slightly better reasoning on ambiguous patterns, but in practice the 8b model matches it on the test cases we've run. Only worth it if you're monitoring containers with complex, multi-stage failure modes.

**Avoid `qwen3:14b`** unless you pin `think: false` at the top level of the Ollama request (not inside `options`). Its thinking mode conflicts with `format: "json"` and returns empty `{}` responses. Dockmon handles this automatically, but if you're debugging raw Ollama calls, be aware.

### Per-Container Model Override

You can assign different models to specific containers in `config.yaml`:

```yaml
containers:
  - name: "critical-database"
    model_override: "qwen2.5:14b"  # use bigger model for important containers
  - name: "nginx-proxy"
    model_override: "llama3.2:3b"  # fast model for simple health checks
```

### Key Architecture Note

Dockmon v5 uses a **structured preprocessor** (`log_analyzer.py`) that handles all mechanical log analysis in Python before the LLM sees anything. The LLM receives a pre-computed summary with severity counts, error deduplication, recovery detection, and a recent log tail — not raw logs. This means:

- Model size matters less than it would with raw log parsing
- Even 3B models get correct results on clear-cut cases
- The LLM only needs to interpret patterns, not parse timestamps or count errors
- Accuracy differences between models show up primarily on ambiguous edge cases

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
Container Logs -> Log Analyzer (timestamp parse, severity count, error dedup, recovery detect)
              -> Structured Summary (stats + deduplicated errors + recent tail)
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
