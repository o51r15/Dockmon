# Dockmon — AI-Driven Container Monitoring System

**Repository:** [https://github.com/o51r15/Dockmon](https://github.com/o51r15/Dockmon)

## Project Roadmap

---

## Architecture Summary

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Excellent Docker & AI library ecosystem |
| Docker SDK | `docker-py` via mounted `/var/run/docker.sock` | Official, well-maintained |
| LLM Interface | HTTP to Ollama API + Pydantic for structured output | Local inference, no API keys |
| Web Backend | FastAPI (async) | Native Pydantic integration, SSE support |
| Web Frontend | HTMX + Alpine.js + Tailwind CSS (CDN) | Zero build step, tiny image footprint |
| Database | SQLite (WAL mode) | No extra dependency; WAL handles concurrent reads from the web UI and writes from the monitor loop |
| Notifications | `apprise` | Single library covers Discord, Slack, Gotify, Telegram, email, and dozens more |

### Project Structure (target)

```
dockmon/
├── sentinel/
│   ├── __init__.py
│   ├── main.py              # Entry point, starts monitor + FastAPI
│   ├── config.py             # YAML config loader + validation
│   ├── db.py                 # SQLite schema, migrations, queries
│   ├── docker_client.py      # Container discovery, log streaming
│   ├── log_pipeline.py       # Pre-filtering, buffering, truncation
│   ├── ai_engine.py          # Prompt construction, Ollama calls, response parsing
│   ├── actions.py            # Restart logic, cooldowns, dry-run mode
│   ├── alerts.py             # Apprise notification wrapper
│   ├── correlator.py         # Multi-container failure correlation
│   ├── digest.py             # Daily digest aggregation + trend analysis
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py         # FastAPI endpoints
│   │   └── events.py         # SSE stream for live dashboard updates
│   └── prompts/
│       ├── v1_evaluate.txt   # Versioned system prompt for log evaluation
│       └── v1_digest.txt     # Versioned prompt for daily digest
├── frontend/
│   ├── index.html            # Dashboard shell
│   ├── explorer.html         # Log explorer view
│   └── static/
│       └── app.js            # Alpine.js components
├── config.example.yaml
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── roadmap.md
└── README.md
```

---

## Phase 0 — Project Scaffolding

**Goal:** Runnable skeleton with config loading and Docker connectivity confirmed.

### Steps

1. **Initialize the repository.** Create the directory structure above. Set up `pyproject.toml` or `requirements.txt` with initial dependencies: `docker`, `fastapi`, `uvicorn`, `pydantic`, `pyyaml`, `httpx`, `apprise`.

2. **Define the config schema.** Create `config.example.yaml`:

   ```yaml
   ollama:
     base_url: "http://ollama:11434"
     default_model: "gemma2:2b"       # lightweight, fast
     digest_model: "llama3:8b"         # heavier model for daily digest
     timeout_seconds: 30

   monitoring:
     poll_interval_seconds: 60
     log_lines_per_check: 200
     dry_run: true                     # evaluate but don't restart

   containers:
     - name: "gluetun"
       enabled: true
       model_override: null
       ignore_patterns:
         - "^\\d{4}/\\d{2}/\\d{2}.*INFO"
         - "Connecting to server"
       compose_group: "vpn-stack"      # restart entire group on failure
     - name: "plex"
       enabled: true
       ignore_patterns:
         - "Scanning library"

   cooldowns:
     initial_minutes: 5
     backoff_multiplier: 3             # 5 → 15 → 45 → 135...
     max_cooldown_minutes: 120
     max_restarts_per_hour: 3          # after this, escalate to alert-only

   alerts:
     urls:
       - "discord://webhook_id/webhook_token"
       - "gotify://hostname/token"

   digest:
     enabled: true
     schedule_cron: "0 7 * * *"        # daily at 7am
   ```

3. **Write `config.py`.** Use Pydantic `BaseSettings` to load and validate the YAML. Every field gets a type and a default. Fail fast on startup if the config is invalid.

4. **Write `docker_client.py` — connection test only.** Connect to the Docker socket, list running containers, print their names. Confirm the SDK works inside the container.

5. **Write `db.py` — schema creation.** Open SQLite in WAL mode. Create tables:

   ```sql
   CREATE TABLE IF NOT EXISTS events (
       id INTEGER PRIMARY KEY,
       container TEXT NOT NULL,
       timestamp TEXT NOT NULL,
       event_type TEXT NOT NULL,       -- 'evaluation', 'restart', 'alert', 'error'
       ai_status TEXT,                 -- 'healthy', 'unhealthy'
       confidence INTEGER,
       root_cause_category TEXT,       -- 'oom', 'network', 'config', 'dependency', 'unknown'
       summary TEXT,
       action_taken TEXT,
       log_snapshot TEXT,              -- pre-restart log lines
       prompt_version TEXT,
       model_used TEXT
   );

   CREATE TABLE IF NOT EXISTS cooldowns (
       container TEXT PRIMARY KEY,
       last_restart TEXT NOT NULL,
       consecutive_restarts INTEGER DEFAULT 0,
       current_cooldown_minutes INTEGER DEFAULT 5,
       alert_only_mode INTEGER DEFAULT 0
   );

   CREATE TABLE IF NOT EXISTS baselines (
       container TEXT PRIMARY KEY,
       healthy_log_sample TEXT,        -- captured on first healthy evaluation
       captured_at TEXT
   );
   ```

6. **Write `Dockerfile` and `docker-compose.yml`.** Mount `/var/run/docker.sock`, the config file, and a volume for the SQLite database. Confirm the container starts, loads config, connects to Docker, and exits cleanly.

### Exit criteria
- `docker compose up` starts the container, prints the list of running containers, and shuts down.

---

## Phase 1 — Log Ingestion & AI Evaluation (Headless MVP)

**Goal:** A loop that streams logs, filters them, sends relevant lines to Ollama, and prints a structured health verdict to stdout.

### Steps

1. **Build `log_pipeline.py` — the pre-filter.**
   - Accept raw log lines from a container.
   - Strip lines matching `ignore_patterns` from the container's config.
   - **Log-level pre-filter:** Parse common log formats (JSON logs, syslog, Python logging, Go's `log` package). Only forward lines at `WARN`/`ERROR`/`FATAL`/unknown-format to the AI. This cuts LLM calls dramatically.
   - Implement a rotating buffer capped at `log_lines_per_check`. If more lines arrive in one interval, keep the most recent N.
   - Return a `LogBatch` Pydantic model: `container_name`, `line_count`, `filtered_lines`, `timestamp_range`.

2. **Build `ai_engine.py` — prompt construction and Ollama integration.**
   - Load the versioned prompt template from `prompts/v1_evaluate.txt`.
   - System prompt structure:
     ```
     You are a container diagnostic engine. You analyze Docker container log
     excerpts and determine whether the container is healthy or failing.

     Respond ONLY with valid JSON matching this schema:
     {
       "status": "healthy" | "unhealthy",
       "confidence": <0-100>,
       "root_cause_category": "oom" | "network" | "config" | "dependency" | "crash" | "none",
       "summary": "<1-2 sentence explanation>",
       "recommended_action": "none" | "restart"
     }
     ```
   - If a baseline exists for the container (from `baselines` table), include it as few-shot context: "Here is an example of normal startup output for this container: ..."
   - Call Ollama's `/api/generate` endpoint via `httpx` with `format: "json"`.
   - Parse the response through a Pydantic model. If parsing fails, retry once. If it fails again, return a fallback `status: "unknown"` result and log the raw response.
   - **Timeout/failure handling:** If Ollama is unreachable or times out, fail open — log the error, skip this evaluation cycle, continue monitoring. Never crash.

3. **Build the main monitor loop in `main.py`.**
   - On startup, enumerate containers matching the config.
   - For each enabled container, call `docker_client` to grab recent logs → pipe through `log_pipeline` → if filtered lines exist, send to `ai_engine`.
   - Print results to stdout in a structured format.
   - Sleep for `poll_interval_seconds`, repeat.

4. **Implement prompt versioning.**
   - Store the prompt filename (e.g., `v1_evaluate.txt`) in every `events` row as `prompt_version`.
   - This lets you correlate diagnostic accuracy with prompt changes over time.

5. **Baseline capture.**
   - On the first evaluation where the AI returns `status: "healthy"` with `confidence >= 80`, save the log sample to the `baselines` table.
   - Subsequent evaluations include this baseline as context.

### Exit criteria
- The container runs continuously, evaluates logs every 60 seconds, and prints JSON verdicts to stdout.
- When Ollama is stopped, the monitor logs a warning and continues without crashing.

---

## Phase 2 — Actions, Cooldowns & Compose Awareness

**Goal:** The system can restart containers (or full compose groups), respects cooldowns with exponential backoff, and sends notifications.

### Steps

1. **Build `actions.py` — the restart engine.**
   - Accept an evaluation result. If `recommended_action == "restart"` and `dry_run == false`:
     a. Check the `cooldowns` table. If the container is still in cooldown or in `alert_only_mode`, skip the restart and send an alert instead.
     b. **Snapshot logs** — save the current log buffer to the `events` table as `log_snapshot`. Post-restart these logs are gone.
     c. Execute `container.restart(timeout=30)` via docker-py.
     d. Wait 10 seconds, check `container.status`. Wait another 20 seconds, check again. If the container is not running after 30 seconds total, mark the restart as failed.
     e. Update the `cooldowns` table: increment `consecutive_restarts`, calculate next cooldown using exponential backoff (`initial * multiplier ^ consecutive`), capped at `max_cooldown_minutes`.
   - If `dry_run == true`: log what *would* happen, send an alert with the recommendation, but take no action.

2. **Implement the boot-loop breaker.**
   - If `consecutive_restarts` for a container reaches `max_restarts_per_hour`, flip `alert_only_mode = 1`.
   - In alert-only mode, the system continues evaluating and alerting but will not restart. The alert message must clearly state: "Container X has been placed in alert-only mode after Y restarts in Z minutes. Manual intervention required."
   - Reset `consecutive_restarts` and `alert_only_mode` when the container has been healthy for 30+ minutes.

3. **Docker Compose group awareness.**
   - Read `compose_group` from the config.
   - When a container in a group needs a restart, restart *all* containers in that group in dependency order.
   - Use `docker compose restart <service>` via subprocess if compose labels are detected, falling back to individual `container.restart()` calls.

4. **Build `alerts.py` — the notification layer.**
   - Initialize `apprise` with the URLs from config.
   - Define alert templates:
     - **Restart executed:** Container name, timestamp, AI summary, root cause category, confidence score, action taken.
     - **Alert-only escalation:** Container name, restart count, recommendation to investigate manually.
     - **Evaluation error:** Ollama unreachable, parse failure, etc.
   - All alerts are also written to the `events` table.

5. **Model selection per container.**
   - If a container has `model_override` in config, use that model. Otherwise use `default_model`.
   - This lets users run lightweight models (gemma2:2b, phi3) for simple services and heavier models for complex ones.

### Exit criteria
- In dry-run mode: evaluates, logs recommendations, sends alerts, never restarts.
- With dry-run off: restarts a test container, confirms it came back up, sends a Discord/Gotify notification.
- A container in a restart loop triggers alert-only mode after 3 restarts.

---

## Phase 3 — Web Interface & Live Dashboard

**Goal:** A browser-based dashboard showing container health, event history, and an interactive log explorer.

### Steps

1. **Build FastAPI routes (`api/routes.py`).**
   - `GET /api/containers` — list all monitored containers with current status (last evaluation result from DB).
   - `GET /api/containers/{name}/logs` — fetch recent logs from Docker, apply the pre-filter, return as JSON.
   - `POST /api/containers/{name}/evaluate` — trigger an on-demand AI evaluation. Return the structured result.
   - `GET /api/events` — paginated event history with filters (container, event_type, date range).
   - `GET /api/events/{container}/restarts` — restart history with the stored log snapshots and AI reasoning.
   - `GET /api/config` — current running configuration (sanitized, no alert URLs).
   - `GET /api/health` — is Ollama reachable, is Docker socket connected, DB stats.
   - `GET /metrics` — Prometheus-format endpoint: restart counts, evaluation latency histogram, LLM call counts, confidence score distribution.

2. **Build SSE stream (`api/events.py`).**
   - `GET /api/stream` — server-sent events endpoint.
   - Push events in real-time: new evaluations, restarts, alert-only escalations, errors.
   - The frontend subscribes on page load — no polling.

3. **Build the dashboard (`frontend/index.html`).**
   - Dark-mode grid of container cards. Each card shows: container name, current status (healthy/unhealthy/unknown), last evaluation time, confidence score, restart count (24h).
   - Cards update live via SSE.
   - Color coding: green (healthy, confidence ≥ 70), yellow (healthy but low confidence, or unknown), red (unhealthy).

4. **Build the log explorer (`frontend/explorer.html`).**
   - Dropdown to select a container.
   - Scrollable log view with syntax highlighting for log levels.
   - "Evaluate with AI" button that sends selected log lines to the `/evaluate` endpoint and displays the structured result inline.
   - **"Why did it restart?"** panel — when viewing a container with recent restarts, show the pre-restart log snapshot and the AI's reasoning side by side.

5. **Build the Dockerfile for the full app.**
   - Multi-stage build: copy frontend assets, install Python deps, run uvicorn.
   - Expose port 8555 (or configurable).

### Exit criteria
- Dashboard loads in a browser, shows live container status, updates without refresh.
- Log explorer can trigger an on-demand evaluation and display results.
- `/metrics` endpoint returns valid Prometheus metrics.

---

## Phase 4 — Daily Digest & Trend Analysis

**Goal:** A scheduled report that summarizes infrastructure health, highlights recurring issues, and surfaces trends.

### Steps

1. **Build `digest.py` — the log aggregator.**
   - Runs on the configured cron schedule (default: daily at 7am).
   - For each monitored container, pull the last 24 hours of logs from Docker.
   - Apply the same pre-filter pipeline from Phase 1.
   - Additionally, for containers with no standard log format, run a small LLM classification pass on a random sample of ~50 lines to catch failures logged at non-standard levels.

2. **Build the trend engine.**
   - Query the `events` table for the past 7 and 30 days.
   - Calculate per-container: restart count (today vs. 7-day avg vs. 30-day avg), most common `root_cause_category`, average confidence score.
   - Flag containers whose restart frequency is increasing week-over-week.

3. **Multi-container failure correlation (`correlator.py`).**
   - When building the digest, check for containers that failed within 60 seconds of each other.
   - Group correlated failures and flag them: "gluetun and sonarr both failed at 03:14 — likely a shared network dependency."
   - Include correlation data in the digest.

4. **Generate the digest.**
   - Send the aggregated data (filtered logs, trends, correlations) to the `digest_model` with a prompt instructing it to produce a structured summary.
   - Output format: a brief overall health score, per-container highlights (only containers with issues), trend warnings, and correlated failure groups.
   - Route through `alerts.py` as a long-form notification.

5. **Store digests in the DB.**
   - New table: `digests (id, timestamp, content_json, model_used, prompt_version)`.
   - Expose via API: `GET /api/digests` — view past digests in the web UI.

### Exit criteria
- Daily digest arrives via Discord/Gotify at the configured time.
- Digest includes trend comparisons ("gluetun: 4 restarts this week, up from 1 last week").
- Correlated failures are grouped and flagged.

---

## Phase 5 — Hardening & Production Readiness

**Goal:** The system is reliable enough to run unattended on a homelab indefinitely.

### Steps

1. **Structured logging for Sentinel itself.** Use Python's `logging` module with JSON output. The monitor should be monitorable — its own logs should be parseable.

2. **Graceful shutdown.** Handle `SIGTERM` / `SIGINT`. Finish the current evaluation cycle, flush DB writes, close the Docker connection, then exit.

3. **DB maintenance.** Add a weekly cleanup job that prunes `events` older than 90 days (configurable) and vacuums the database.

4. **Config hot-reload.** Watch the config file for changes. On modification, reload without restarting the container. Log what changed.

5. **Error budget tracking.** If the AI engine fails more than 5 consecutive times (Ollama down, parse errors), pause monitoring and send a critical alert. Resume automatically when Ollama responds to a health check.

6. **Write tests.**
   - Unit tests for the pre-filter pipeline with sample logs from common containers (nginx, Plex, Sonarr, gluetun).
   - Integration test that spins up a test container, injects error logs, and verifies the evaluation → restart → alert pipeline end-to-end.
   - Mock Ollama responses for deterministic testing.

7. **Documentation.** Write `README.md` with setup instructions, config reference, and example `docker-compose.yml` for common homelab setups.

### Exit criteria
- The system runs for 7 days unattended without crashes or unexpected behavior.
- All tests pass. README is complete.

---

## Future Enhancements (Post-v1)

These are not part of the initial build but are natural extensions once the core is stable:

- **Self-healing scripts** — Instead of just restarting, allow custom remediation scripts per container (e.g., delete a lock file, clear a cache directory, then restart).
- **Community prompt library** — Application-specific prompt templates (tuned for nginx, Plex, *arr apps, databases) loadable from a GitHub repo.
- **Resource metric correlation** — Pull CPU/memory stats from the Docker API. If the AI sees failure logs *and* the container is at 99% memory, boost the confidence score.
- **Multi-host support** — Monitor containers across multiple Docker hosts via TCP socket or SSH tunnel.
- **Web-based config editor** — Edit `config.yaml` from the dashboard with validation and hot-reload.
- **Ollama model auto-selection** — Let the system benchmark available models on startup and pick the fastest one that meets a quality threshold.

---

## Timeline Estimate

| Phase | Scope | Estimated Effort |
|---|---|---|
| Phase 0 | Scaffolding, config, Docker connection, DB schema | 1–2 days |
| Phase 1 | Log pipeline, AI engine, monitor loop, baselines | 3–4 days |
| Phase 2 | Restarts, cooldowns, compose groups, alerts, dry-run | 3–4 days |
| Phase 3 | FastAPI, SSE, dashboard, log explorer, metrics | 4–5 days |
| Phase 4 | Digest, trends, correlation | 2–3 days |
| Phase 5 | Hardening, tests, docs | 2–3 days |
| **Total** | | **~15–21 days** |

These estimates assume solo development with a few hours per day. The phases are designed to be independently testable — each one produces a working system you can run and validate before moving on.
