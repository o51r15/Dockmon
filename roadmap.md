# Dockmon Roadmap

## Completed

### Phase 0 -- Project Scaffolding
- Project structure, config schema (Pydantic), Docker SDK connection, SQLite WAL-mode DB, Dockerfile, docker-compose.yml

### Phase 1 -- Log Ingestion & AI Evaluation
- Log pre-filter pipeline (ANSI strip, Docker timestamp strip, level detection, ignore patterns)
- AI evaluation engine (Ollama /api/generate with structured JSON)
- Versioned prompt templates (prompts/v1_evaluate.txt)
- Monitor loop with baseline capture

### Phase 2 -- Actions, Cooldowns & Alerts
- Restart engine with dry-run mode (default)
- Exponential backoff cooldowns (5m -> 15m -> 45m -> 120m cap)
- Boot-loop breaker (alert-only mode after max_restarts_per_hour)
- Apprise notification layer (restart, dry-run, escalation, cooldown, error alerts)
- Pre-restart log snapshot storage

### Phase 3 -- Web Interface
- FastAPI REST API (containers, logs, evaluate, events, config, health)
- SSE event stream for live dashboard updates
- Dashboard with container status grid, live evaluation results
- Log explorer with raw/filtered toggle, on-demand AI eval, restart history

### Phase 4 -- Daily Digest
- Digest engine querying 24h of events per container
- LLM-generated summary with fleet health score, trends, recommendations
- Cron-based scheduler with configurable time
- On-demand digest via POST /api/digest

### Phase 5 -- Hardening
- Per-container error isolation (one failure does not stop the cycle)
- Ollama connectivity check at startup
- Updated Dockerfile/docker-compose for port 8556
- Updated README with full feature list, config reference, API docs

## Future

- Compose group-aware restarts -- restart entire compose stacks when a member fails
- Multi-container failure correlation -- detect containers failing within 60s of each other
- Trend engine -- 7-day and 30-day restart frequency tracking
- DB maintenance -- auto-prune events older than 90 days
- Config hot-reload -- watch config file for changes, reload without restart
- Prometheus metrics -- /metrics endpoint for external monitoring
- Self-healing scripts -- custom remediation per container
- Resource metric correlation -- CPU/memory stats from Docker API
- Multi-host support -- monitor across Docker hosts via TCP/SSH
- Web config editor -- edit config.yaml from the dashboard
