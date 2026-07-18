# Dockmon

AI-driven Docker container monitoring with automatic diagnostics and self-healing.

> **⚠️ Alpha — not yet functional.** This project is under active development. Phase 0 (scaffolding) is complete. The monitor loop, AI evaluation, and web dashboard are not yet implemented.

## What it will do

- Stream Docker container logs and evaluate them with a local LLM (Ollama)
- Detect failures, classify root causes, and optionally restart unhealthy containers
- Exponential backoff cooldowns with boot-loop protection
- Docker Compose group-aware restarts
- Web dashboard with live status updates (SSE)
- Interactive log explorer with on-demand AI evaluation
- Daily digest with trend analysis and correlated failure detection
- Notifications via Discord, Gotify, Telegram, email, and more (Apprise)

## Current status

- [x] Project structure
- [x] Config loader with Pydantic validation
- [x] Docker SDK connection
- [x] SQLite database with WAL mode
- [x] Dockerfile and docker-compose.yml
- [ ] Log ingestion pipeline
- [ ] AI evaluation engine (Ollama)
- [ ] Restart actions and cooldowns
- [ ] Alerting
- [ ] Web API (FastAPI)
- [ ] Dashboard frontend
- [ ] Daily digest

## Quick start (once functional)

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your containers and Ollama URL
docker compose up -d
```

## Requirements

- Docker host with `/var/run/docker.sock` accessible
- Ollama instance running locally or on the network
- Python 3.12+ (if running outside Docker)

## License

MIT
