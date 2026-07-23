# DockLlama Roadmap (formerly Dockmon)

## How To Use This Roadmap

When the user says "lets start the roadmap," find the phase marked **⬅️ START HERE** and begin working on it sub-phase by sub-phase. Follow this workflow for EVERY change:

1. **Inspect** — Read the current source files involved. Understand what exists before touching anything.
2. **Build** — Write the code change. Use Python patch scripts (pscp to `/tmp/`, execute remotely) for complex edits.
3. **Review** — Syntax check (`ast.parse()`), read the modified file back, verify the diff is correct.
4. **Test** — Commit, push, rebuild container (`docker build -t ghcr.io/o51r15/dockmon:dev .`), restart, check logs, hit the API, verify the change works end-to-end.
5. **Review** — Confirm test results. Check for regressions. If something broke, fix it before moving on.
6. **Move to next** — Mark the sub-phase done in this file, then start the next one.

Do NOT batch multiple sub-phases into one commit. One sub-phase = one commit = one test cycle.

---

## Pre-Roadmap: Rename Dockmon → DockLlama ✅ DONE (Session 4)
Renamed across 32 files — Python package dir, imports, Docker image, CI/CD, compose, all user-facing strings.

---

## Completed (Phases 0–5)

### Phase 0 — Project Scaffolding
- Project structure, config schema (Pydantic), Docker SDK connection, SQLite WAL-mode DB, Dockerfile, docker-compose.yml

### Phase 1 — Log Ingestion & AI Evaluation
- Log pre-filter pipeline (ANSI strip, Docker timestamp strip, level detection, ignore patterns)
- AI evaluation engine (Ollama /api/generate with structured JSON)
- Versioned prompt templates (v1 through v5)
- Monitor loop with baseline capture

### Phase 2 — Actions, Cooldowns & Alerts
- Restart engine with dry-run mode (default)
- Exponential backoff cooldowns (5m → 15m → 45m → 120m cap)
- Boot-loop breaker (alert-only mode after max_restarts_per_hour)
- Apprise notification layer (restart, dry-run, escalation, cooldown, error alerts)
- Pre-restart log snapshot storage

### Phase 3 — Web Interface
- FastAPI REST API (containers, logs, evaluate, events, config, health)
- SSE event stream for live dashboard updates
- Dashboard with container status grid, live evaluation results
- Log explorer with raw/filtered toggle, on-demand AI eval, restart history

### Phase 4 — Daily Digest
- Digest engine querying 24h of events per container
- LLM-generated summary (gemma4) with fleet health, trends, recommendations
- Cron-based scheduler with configurable time
- On-demand digest via POST /api/digest
- Digest storage in DB with historical retrieval API
- Digest viewer page with date picker

### Phase 5 — Hardening & Scale
- Per-container error isolation (one failure does not stop the cycle)
- Ollama connectivity check at startup
- v5 structured preprocessor (Python handles mechanical analysis, LLM interprets)
- Auto-healthy fast path (skip LLM when all logs match ignore patterns)
- Robust digest JSON parsing with `_extract_json()` fallback
- Expanded from 8 to 15 containers
- GitHub Actions CI/CD (push → :dev, release → :latest + semver)
- Separated local config from git template
- Settings page (Apprise notification management)
- Compose group-aware restarts
- Trend engine (7d/30d stats with period-over-period comparison)
- DB maintenance (auto-prune events older than retention_days)

---

## Upcoming

**Recommended execution order across phases:** Phase 7 (telemetry, read-only enrichment) → Phase 8.1–8.3 (context injection, prompt-level) → Phase 6.2 (dependency groups, touches restart logic) → Phase 8.4 (prompt UI) → Phase 9 (model validation) → Phase 7B (stats history) → Phase 6.5-6.6 (health checks, blackout windows) → Phase 10+ (learning mode, fleet, UI improvements).

### Phase 6 — Advanced Remediation & Alerting

**Objective:** Move beyond simple restarts by making the system aware of stack dependencies and allowing custom recovery actions.

#### 6.1 Persisted Notifications ✅ DONE (Session 3, commit `6bee8b0`)
Added `alert_urls` table to SQLite. PUT /api/alerts saves to DB, GET reads from DB. Startup merges DB URLs with config.yaml URLs. Tested: add URL → restart container → URL persists.

#### 6.2 Docker Compose Dependency Awareness ✅ DONE (Session 5)
Restarting `gluetun` breaks network routing for `qbittorrent` unless both are restarted in the correct order. Rather than waiting for the AI to flag dependent containers as unhealthy minutes later, Dockmon should execute coordinated restarts automatically.

**Config approach:** Use an explicit `dependency_groups` block rather than Docker Compose labels, since users often link containers across different compose files:

```yaml
# config.yaml addition
dependency_groups:
  vpn_stack:
    - gluetun
    - qbittorrent
    - prowlarr
  database_stack:
    - pinchfork-db
    - pinchfork
```

**Implementation:**
- Before restarting a container, cross-reference its name against `dependency_groups` in `actions.py`
- If the container belongs to a group, restart all members in the order defined in the array
- Log individual cooldown entries for every container restarted as part of a group to prevent cascading restart loops
- Note: basic `compose_group` co-restarts already exist — this adds explicit ordering and cross-compose-file dependency awareness

#### 6.3 Custom Remediation Scripts
Expand the LLM schema beyond `recommended_action: "restart" | "none"`.

- Allow `recommended_action: "script_name"` mapping to predefined bash scripts in a `/app/scripts/` volume
- Enables clearing lock files, flushing caches, recreating containers rather than just restarting

#### 6.4 Multi-Container Failure Correlation
Detect containers failing within 60s of each other and surface correlated failures in the dashboard and digest.


#### 6.5 HTTP Health Checks *(inspired by darthnorse/dockmon)*

External endpoint monitoring — complementary to our AI-driven log evaluation. Some containers expose `/health` or `/api/status` endpoints that definitively answer "is this service working?" without needing log analysis.

- New `health_checks` table: `(container TEXT PK, url TEXT, method TEXT DEFAULT 'GET', expected_status INT DEFAULT 200, interval_seconds INT DEFAULT 60, timeout_seconds INT DEFAULT 10, failure_threshold INT DEFAULT 3, enabled BOOL DEFAULT 0)`
- `GET/PUT /api/containers/{name}/healthcheck` — configure per-container HTTP checks
- Background task polls endpoints at configured interval
- After `failure_threshold` consecutive failures → trigger the same action pipeline as an unhealthy AI eval (respect cooldowns, dependency groups, etc.)
- Health check status shown on dashboard card (green/yellow/red dot separate from AI health score)
- **DockLlama twist:** Feed health check failures INTO the AI eval context: "HTTP health check at /health returned 503 (3 consecutive failures)" — the AI sees both logs AND endpoint status


#### 6.6 Blackout Windows (Maintenance Schedules) *(inspired by darthnorse/dockmon)*

Suppress alerts and auto-restarts during scheduled maintenance periods.

- New `blackout_windows` table: `(id INTEGER PK, name TEXT, days TEXT, start_time TEXT, end_time TEXT, enabled BOOL DEFAULT 1)`
- `days` is JSON array of weekday ints (0=Mon, 6=Sun); supports overnight spans (start > end)
- Settings UI: add/edit/delete blackout windows under Settings → Notifications
- During active blackout: skip alert notifications, defer auto-restarts, still log events and run evals
- After blackout ends: post-blackout health check scans all containers, sends deferred alerts for any in failed state
- Dashboard banner when blackout is active: "Maintenance window active: [name]"
- **DockLlama twist:** AI evals still run during blackout (data collection continues), but actions are suppressed. The digest includes a "during maintenance" section summarizing what happened while alerts were off.


---

### Phase 7 — Telemetry & Resource Correlation ✅ DONE (Session 4)

**Status:** COMPLETE
**Objective:** Enrich the LLM's context window by feeding it live CPU and memory metrics alongside log summaries. This is the most effective way to eliminate false positives — "OOM" logs are meaningless if the container has 8GB free RAM, but critical if it's pinned at 100%.

**Why this goes first:** Telemetry is read-only enrichment with zero risk to existing behavior. Dependency groups (6.2) touch restart logic and should wait.

#### 7.1 Metrics Pipeline
Fetch stats via `container.stats(stream=False)` in `docker_client.py`. Docker's raw nanosecond counters require delta calculation:

```python
stats = container.stats(stream=False)

# Memory
mem_usage = stats['memory_stats']['usage']
mem_limit = stats['memory_stats']['limit']
mem_percent = (mem_usage / mem_limit) * 100.0

# CPU (delta between current and previous reading)
cpu_delta = (stats['cpu_stats']['cpu_usage']['total_usage']
             - stats['precpu_stats']['cpu_usage']['total_usage'])
system_delta = (stats['cpu_stats']['system_cpu_usage']
                - stats['precpu_stats']['system_cpu_usage'])
num_cpus = stats['cpu_stats'].get('online_cpus',
           len(stats['cpu_stats']['cpu_usage'].get('percpu_usage', [1])))
cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0
```

- Log metrics to console first and verify they match `docker stats` CLI output before integrating

#### 7.2 Preprocessor Integration
- Add `cpu_percent` and `mem_percent` floats to the `LogSummary` dataclass in `log_analyzer.py`
- Place metrics at the top of `.to_prompt()` output for maximum LLM attention:
```
Container: qbittorrent
Time Window: 15 minutes
Resource Usage: CPU 8.4% | RAM 42.1%
Log Severities: 4 ERROR, 12 WARN
Deduplicated Errors: ...
```

#### 7.3 Prompt Engineering Adjustments
Add explicit correlation rules to `v5_evaluate.txt`:
- "You are provided with current CPU and RAM usage percentages. Correlate these metrics with the log events."
- "High RAM usage (>90%) alongside 'Out of Memory' or 'Killed' logs indicates a critical failure requiring a restart."
- "High CPU usage (>95%) alongside 'Timeout' or 'Deadlock' logs indicates a hung process requiring a restart."
- Run a manual evaluation against a noisy container to verify LLM confidence scores improve with the added context

### Phase 7B — Stats History & Resource Charts *(inspired by darthnorse/dockmon)*

**Status:** NOT STARTED
**Objective:** Persist CPU, memory, and network metrics over time so the dashboard shows historical resource usage, not just point-in-time snapshots. Currently Phase 7 fetches stats per eval cycle but discards them — this phase stores them and makes them browseable.

#### 7B.1 Stats Persistence Table
- New `container_stats` table: `(container TEXT, timestamp TEXT, cpu_percent REAL, mem_percent REAL, mem_usage_mb REAL, net_rx_bytes INT, net_tx_bytes INT)`
- Record one row per container per eval cycle (piggyback on existing `get_container_stats()` call)
- Configurable retention: `stats_retention_days` in config.yaml (default 7, max 90)
- Auto-prune old rows in the existing DB maintenance task

#### 7B.2 Stats API Endpoints
- `GET /api/containers/{name}/stats?range=1h|24h|7d|30d` — returns time-series data for charts
- `GET /api/stats/fleet?range=24h` — aggregate CPU/RAM across all containers (for fleet overview)
- Downsample data for longer ranges (1-min resolution for 1h, 5-min for 24h, 1-hour for 7d+)

#### 7B.3 Stats Charts in Dashboard
- Add sparkline mini-charts to container cards on Dashboard (CPU + RAM, last 1h)
- Container detail view (click card → modal/drawer) with full-size charts:
  - CPU usage over time
  - Memory usage over time
  - Network I/O over time
- Time-range selector: 1h / 24h / 7d / 30d
- Use lightweight charting (Chart.js via CDN or inline SVG sparklines for Alpine.js)

#### 7B.4 Fleet Resource Overview
- Add a "Resource Usage" summary card to Dashboard header: total CPU %, total RAM used/available
- Historical fleet-level charts on Insights → Health Trends page

---

### Phase 8 — Intelligent Evaluation: From Data Deletion to Context Injection

**Status:** 8.1-8.4 COMPLETE (Sessions 4-5), 8.5 NOT STARTED
**Objective:** Transition from relying on `ignore_patterns` (which blindfold the AI) to teaching the AI what it's looking at. Three architectural methods work together to eliminate false positives while preserving the AI's ability to detect real problems.

**Core principle:** Never delete data from the AI's view. Instead, add context so it understands what "normal" looks like for each specific container.

#### 8.1 Dynamic System Context (Container-Specific Prompts) ✅ DONE (Session 4)

When the evaluation pipeline detects a specific workload type (PostgreSQL, VPN, torrent client, media server), it appends a targeted knowledge block to `v5_evaluate.txt` before sending to the LLM. This block defines acceptable behaviors that would otherwise look like failures.

**Config approach:** Add a `context_prompt` field to `ContainerConfig` in `config.yaml`:

```yaml
containers:
  - name: "pinchfork-db"
    enabled: true
    context_prompt: |
      This is a PostgreSQL database serving the Pinchfork application.
      Rule 1 - Database Transaction Collisions: Log entries displaying
      "deadlock detected" or "SQLSTATE 40P01" represent routine transaction
      collisions during concurrent writes. The database intentionally
      terminates one query to protect data integrity. This is a built-in
      safety mechanism. Do not lower the health score.
      Rule 2 - Administrative Shutdowns: Messages stating "received fast
      shutdown request" or "terminating connection due to administrator
      command" indicate a clean, intentional stop. Do not penalize the score.

  - name: "gluetun"
    enabled: true
    context_prompt: |
      This is a VPN tunnel container. Network interruptions during server
      rotation are expected. DNS resolution failures lasting under 30 seconds
      followed by successful connections are normal VPN handoff behavior.
```

**Implementation path:**
- Add `context_prompt: Optional[str] = None` to `ContainerConfig` in `config.py`
- In `ai_engine.py` `_build_messages()`, if the container has a `context_prompt`, append it to the system prompt after the base `v5_evaluate.txt` content: `system_prompt += "\n\n## Container-Specific Context\n" + ctx.context_prompt`
- Add `context_prompt` field to `EvaluationContext` dataclass
- Pass `container_cfg.context_prompt` through from `_process_container()` in `main.py`
- This is a ~15 line change across 3 files with zero risk to existing behavior (None = no change)

#### 8.2 Few-Shot Learning (Example-Based Calibration) ✅ DONE (Session 4)

Language models learn by example. Provide a small reference section inside the prompt containing a sample log of a known false positive alongside the correct analysis. By showing the model a benign shutdown sequence scored as healthy, it maps that logic to live data.

**Config approach:** Add an optional `examples` list to `ContainerConfig`:

```yaml
containers:
  - name: "pinchfork-db"
    enabled: true
    examples:
      - label: "Normal PostgreSQL shutdown (healthy)"
        log_snippet: |
          FATAL: terminating connection due to administrator command
          LOG: received fast shutdown request
          LOG: aborting any active transactions
          LOG: background worker "logical replication launcher" (PID 82) exited with exit code 1
          LOG: shutting down
          LOG: database system is shut down
        correct_score: 95
        correct_status: "healthy"
        reasoning: "This is a clean administrative shutdown. All FATAL messages are expected during this sequence."
```

**Implementation path:**
- Add `examples: list[dict] = []` to `ContainerConfig`
- In `_build_messages()`, format examples as a `## Reference Examples` block appended after the container context:
  ```
  ## Reference Examples
  The following are known scenarios with their correct evaluations. Use these to calibrate your scoring.
  
  ### Example: Normal PostgreSQL shutdown (healthy)
  Log sample:
  ---
  FATAL: terminating connection due to administrator command
  ...
  ---
  Correct assessment: healthy, score 95
  Reasoning: This is a clean administrative shutdown.
  ```
- Few-shot examples are the most effective way to calibrate small models like llama3.1:8b — they often outperform lengthy rule descriptions

#### 8.3 Contextual Metadata Tagging (Preprocessor Annotations) ✅ DONE (Session 4)

Rather than altering the LLM prompt, alter the data payload. Add a lightweight preprocessing step in `log_analyzer.py` that scans for known administrative commands or expected patterns. When it finds one, it appends a metadata note to that log line so the AI reads the raw log and the context simultaneously.

**Config approach:** Add a `known_patterns` list to `ContainerConfig`:

```yaml
containers:
  - name: "pinchfork-db"
    enabled: true
    known_patterns:
      - pattern: "FATAL.*terminating connection due to administrator command"
        tag: "[ROUTINE: admin shutdown sequence]"
      - pattern: "deadlock detected"
        tag: "[ROUTINE: normal transaction collision, auto-resolved]"
      - pattern: "server misbehaving"
        tag: "[EXPECTED: transient DNS during container startup]"
```

**Implementation path:**
- Add `known_patterns: list[dict] = []` to `ContainerConfig`
- In `log_analyzer.py` `analyze_logs()`, after cleaning each line but before severity detection, check against `known_patterns`. If matched, append the tag: `"FATAL: terminating connection due to administrator command [ROUTINE: admin shutdown sequence]"`
- The tagged lines flow through `to_prompt()` into the error patterns and recent tail sections — the AI sees both the raw event and the human-provided context
- Tags also influence the `ErrorPattern.context` field, replacing the generic "During shutdown sequence" with the user's specific annotation

#### 8.4 Prompt Storage & Management UI ✅ DONE (Session 5)

Once container-specific prompts are working from config, move them into the database for live editing without container restarts.

- New `container_prompts` table: `(container TEXT PRIMARY KEY, context_prompt TEXT, examples TEXT, known_patterns TEXT, updated_at TEXT)`
- API endpoints: `GET/PUT /api/containers/{name}/prompt` for reading and updating per-container prompt configuration
- "Prompt Editor" page in the frontend per container — textarea for context prompt, structured form for examples and known patterns
- "Test" button: re-evaluates the container's most recent log snapshot with the edited prompt and shows the result side-by-side with the current evaluation
- Prompts in DB override `config.yaml` values (DB wins, config is the fallback default)

#### 8.5 Workload Auto-Detection (Stretch)

Instead of requiring manual `context_prompt` configuration, detect common workload types from container image names and log signatures, then auto-inject a community-maintained context block.

- Map known images to workload types: `postgres:*` → PostgreSQL, `linuxserver/plex` → Plex Media Server
- Ship a `workload_contexts/` directory with default context prompts per workload type
- Auto-detected context is appended BEFORE any user-supplied `context_prompt` (user rules always win)
- Dashboard shows detected workload type with an "Edit" link to customize

---

### Phase 9 — Model Validation & Hardware-Aware Scheduling ⬅️ START HERE

**Status:** NOT STARTED
**Objective:** Eliminate guesswork from model selection and poll interval configuration. The settings page should validate that a model actually works before allowing it, benchmark its speed on the user's hardware, and calculate a safe poll interval automatically.

#### 9.1 Model Discovery & Selection UI

New "Model Configuration" section on the Settings page.

- On page load, query Ollama `GET /api/tags` to list all available models
- Display models in a dropdown/selector with size and quantization info
- "Save" button stays **grayed out** until the model passes validation (9.2)
- Previously tested and validated models are listed as **"Supported"** — these can be selected without re-testing
- Untested models show a warning: "This model has not been validated. Quality of results may vary with unsupported models."
- Store tested model results in a new `tested_models` DB table: `(model TEXT PK, tested_at TEXT, healthy_pass BOOL, failing_pass BOOL, avg_response_ms INT, status TEXT)` where status is "supported" or "untested"

#### 9.2 Model Validation Testing

Before a model can be saved as the default, it must pass two hardcoded test fixtures:

- **Healthy test:** A known-good log summary (clean PostgreSQL checkpoint sequence). Expected result: `status: "healthy"`, `health_score >= 80`.
- **Failing test:** A known-bad log summary (OOM crash loop with no recovery). Expected result: `status: "unhealthy" or "critical"`, `health_score < 40`.
- Both tests run sequentially. If the model returns no result or times out → **fail**, suggest a different model.
- If results come back but scores are wrong (healthy test scored unhealthy, or failing test scored healthy) → **fail**, display what the model returned vs. what was expected, suggest trying a different model.
- Response time from both tests is recorded and averaged — this is the per-container eval time used for interval calculation (9.3).

**Advanced mode:** Users can also paste their own log samples and define expected outcomes to test models against real-world scenarios specific to their stack.

#### 9.3 Hardware-Aware Poll Interval Calculation

Once a model passes validation, its average response time unlocks the interval configuration:

- **Base calculation:** `safe_interval = (avg_response_time × container_count) + 300s` (5 minute buffer)
- Example: 11s avg × 15 containers = 165s work + 300s buffer = 465s (~7.75 min) safe interval
- A **slider** appears showing the interval range with color coding:
  - 🔴 **Red:** interval < total work time (hardware cannot keep up, evaluations will overlap)
  - 🟡 **Yellow:** interval < work time + 2 min (high load, minimal breathing room)
  - 🟢 **Green:** interval >= work time + 5 min (safe, recommended zone)
- Default position: start of green zone
- User can drag into yellow (with warning) but not into red

**Per-container overrides:** If a container has a different `model_override` or custom send interval, that container's eval time uses its own model's benchmark rather than the system default. The total work time calculation sums each container's individual expected eval time.

#### 9.4 Dynamic Recalculation

- When containers are added or removed from monitoring, display a banner: "Container count changed. Recalculating recommended interval..." with a "Retest" button
- Retest is **suggested but not required** — the system recalculates using the existing per-model benchmark times and the new container count
- If a container specifies a `model_override` for a model that hasn't been tested yet, that model must pass validation (9.2) before the container can be enabled
- Store interval calculation inputs in DB so they survive restarts: `(model, avg_ms, container_count, calculated_interval, user_override, updated_at)`

#### 9.5 First-Run Setup Wizard (Stretch)

Optional guided setup shown when DockLlama starts with no config or an empty container list:

1. **Ollama connection** — enter URL, test connectivity, show available models
2. **Model selection** — pick a model, run validation tests (9.2)
3. **Container selection** — show running Docker containers, pick which to monitor
4. **Interval recommendation** — auto-calculate and display the slider (9.3)
5. **Save** — write config and start monitoring

If the wizard is skipped, all of this is accessible from the Settings page.

---

### Phase 10 — Learning Mode (formerly Phase 9)

The core problem with generic AI healthchecks is that every container has its own definition of "normal." Learning Mode lets Dockmon observe containers passively, surface patterns it thinks might be problems, and learn from user feedback what actually matters for each container.

#### L1 — Pattern Detection Engine
- New LLM prompt: `v1_learn.txt` — focused on pattern detection, not health judgment
- New DB tables: `learned_patterns`, `container_rules`
- Pattern deduplication via LLM clustering of similar log lines
- Frequency and first/last seen tracking

#### L2 — Learning Mode Dashboard
- New frontend page: `/learning.html`
- Per-container pattern list with examples, frequency, AI guess
- Approve/reject/watch buttons per pattern
- API endpoints for pattern listing, verdict submission, rule viewing, promotion

#### L3 — Rule Injection into Evaluation
- Append learned rules to evaluation prompts as container-specific context
- Rules act as few-shot examples: "The user has told you that X is normal and Y is a problem"
- Track whether learned rules improve evaluation accuracy
- Allow editing/deleting rules from the dashboard

#### L4 — Continuous Learning
- Flag NEW patterns not seen during learning, even in monitoring mode
- Periodic notifications: "I noticed a new pattern in gluetun I haven't seen before..."
- Re-enter learning mode to refine rules
- Export/import rules between containers with similar roles

#### Config Changes
```yaml
containers:
  - name: "gluetun"
    enabled: true
    mode: "learning"          # "learning" | "monitoring" (default: monitoring)
    learning:
      min_patterns_before_promote: 10
      observation_cycles: 100
    rules_file: null           # auto-generated, or path to manual rules yaml
```

#### Key Design Decisions
- Learning mode uses a SEPARATE prompt from evaluation — it's identifying patterns, not judging health
- Pattern detection runs through the LLM, not regex — clusters similar messages as the same pattern
- User verdicts are stored permanently and survive prompt version upgrades
- Rules are human-readable YAML, editable by hand
- A container can stay in learning mode indefinitely

---

### Phase 11 — Fleet Management (Distributed Architecture)

**Objective:** Scale Dockmon to monitor multiple servers across an entire homelab or production environment.

#### 11.1 Remote Socket Integration
- Refactor `docker_client.py` to support multiple Docker clients
- Beyond `/var/run/docker.sock`, support TCP (`tcp://192.168.1.50:2375`) and SSH endpoints in `config.yaml`
- Update database schema to include `node_name` on all events and cooldowns

#### 11.2 UI Fleet Grouping
- Dashboard groups containers by host node
- Log Explorer dropdown to filter by node

#### 11.3 Portainer API Sync (Stretch)
- Accept a Portainer API token, auto-discover all running edge nodes, dynamically map target containers
- Eliminates manual IP configuration in `config.yaml`

---

### Phase 12 — Open-Source Readiness

**Objective:** Prepare the repository for public consumption and community contributions.

- **Community Prompt Library:** Public GitHub repo of tuned prompts (`prompts/nginx.json`, `prompts/plex.json`). Button in the UI to "Fetch Community Prompts"
- **Privilege Dropping:** Dockerfile runs as non-root user (Docker socket access via group permissions)
- **Documentation:** Comprehensive README covering Ollama setup, v5 preprocessor architecture, config reference, API docs
- **Config hot-reload:** Watch config file for changes, reload without restart
- **Prometheus metrics:** `/metrics` endpoint for external monitoring (Grafana dashboards)

### Phase 13 — Dashboard & UI Improvements *(inspired by darthnorse/dockmon)*

**Status:** NOT STARTED
**Objective:** Elevate the dashboard from a health grid to a full container management interface. Borrow proven UI patterns while keeping DockLlama's AI-first identity.

#### 13.1 Container Detail View
- Click any container card → opens a detail modal/drawer with tabs:
  - **Overview:** AI health score, last eval summary, uptime, image, created date
  - **Stats:** CPU/RAM/Network charts (from Phase 7B) with time-range selector
  - **Logs:** Embedded log viewer (raw + filtered, like current Log Explorer but scoped to one container)
  - **Events:** Event history for this container (restarts, evals, alerts)
  - **AI Config:** Quick access to prompt editor (context_prompt, examples, known_patterns)
- Deep-linkable: `/dashboard?container=gluetun` opens the detail view directly

#### 13.2 Container Tagging
- Auto-derive tags from Docker labels (`com.docker.compose.project` → project tag)
- User-defined tags via API and Settings UI
- Dashboard filter bar: filter container grid by tag (e.g., show only "vpn_stack" containers)
- Tags stored in DB: `container_tags (container TEXT, tag TEXT, source TEXT, PRIMARY KEY(container, tag))`

#### 13.3 Event Viewer Improvements
- Full-text search across events
- Filter by event type (restart, eval, alert, action)
- Filter by severity
- Clickable container names → jump to container detail
- Pagination for large event histories
- Export events as CSV

#### 13.4 Dashboard KPI Bar
- Summary bar at top of Dashboard: total containers, running/stopped counts, avg fleet health score, active alerts count
- Collapsible (user preference saved in localStorage)

#### 13.5 Container Actions from Dashboard
- Start/stop/restart buttons directly on container cards (for stopped containers)
- "Re-evaluate now" button → triggers immediate AI eval for a single container
- Action confirmation modals for destructive operations


---

## Other Ideas (Unscheduled)

- Container image update detection: check Docker Hub/GHCR for newer image digests, notify via alerts *(from dockmon)*
- Bulk container operations: start/stop/restart multiple containers at once from dashboard *(from dockmon)*
- Anomaly detection: statistical baseline deviation alerting without LLM involvement
- Web config editor: edit config.yaml from the dashboard
- Webhook integrations: accept incoming webhooks to trigger evaluations
- Container grouping by service role (databases, web servers, media) for role-specific prompts
