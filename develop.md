# DockLlama — Developer Log & Handoff

**Last updated:** July 23, 2026 (Session 6)
**Repository:** https://github.com/o51r15/DockLlama (renamed from DockLlama)
**Status:** Running in dry-run mode as Docker container, monitoring 15 production containers
**Latest commit:** `7487933` — Restructure nav: Dashboard, Insights (sidebar), Settings (sidebar)

## FIRST TASK: Complete Rename ✅ DONE (Session 4)
Renamed dockmon → dockllama across 32 files including Python package dir, imports, Docker image, CI/CD, compose, all user-facing strings.

---

## Quick Start for New Chat Sessions

When the user says **"lets start the roadmap"**:

1. Read `roadmap.md` (in the same directory as this file)
2. Find the phase marked **⬅️ START HERE**
3. Work through it sub-phase by sub-phase using the workflow below
4. After completing a phase, move the **⬅️ START HERE** marker to the next phase

**Development workflow for EVERY change — no exceptions:**

1. **Inspect** — Read the source files you're about to modify. Understand what exists.
2. **Build** — Write the code change. For complex edits, write a Python patch script, pscp it to `/tmp/` on the server, execute remotely.
3. **Review** — Syntax check with `ast.parse()`. Read the modified file back. Verify the diff is what you intended.
4. **Test** — Commit, push, rebuild container, restart, check logs, hit the API, verify the change works end-to-end.
5. **Review** — Confirm test results. Check for regressions. Fix before moving on.
6. **Move to next** — Update the roadmap status, then start the next sub-phase.

**One sub-phase = one commit = one test cycle.** Do NOT batch multiple sub-phases.

---

## What Is Dockmon

Dockmon is an AI-driven Docker container health monitoring system. It polls container logs, runs them through a structured Python preprocessor, sends summaries to a local LLM (Ollama), and takes action (restart, alert, or dry-run log) based on the AI's health assessment. It has a web dashboard, daily digest reports, and Apprise-based notifications.

**Stack:** Python 3.14, FastAPI, Alpine.js, SQLite (WAL), Ollama (llama3.1:8b default, gemma4 digest)

---

## Infrastructure

| Component | Location | Details |
|-----------|----------|---------|
| **Dockmon code** | `/home/o51r15/scripts/dockmon/` on Optiplex server | Python 3.14, runs as Docker container |
| **Optiplex server** | `192.168.1.192` | Runs Docker containers + Dockmon |
| **GPU server** | `192.168.1.125` | Runs Ollama (LLM inference) |
| **Ollama API** | `http://192.168.1.125:11434` | llama3.1:8b (eval), gemma4:latest (digest) |
| **Dockmon Web UI** | `http://192.168.1.192:8556` | FastAPI + static HTML |
| **GitHub repo** | `ghcr.io/o51r15/dockmon` | Docker images via GitHub Actions |
| **SSH access** | PuTTY (plink/pscp) via Desktop Commander MCP | **NEVER use bash sandbox for SSH** |

### SSH Commands (CRITICAL)

The `mcp__workspace__bash` tool runs in an isolated Linux sandbox — it CANNOT reach the server. All server operations MUST use Desktop Commander MCP tools (load via ToolSearch: `select:mcp__Desktop_Commander__start_process`):

```
# Run command on server
C:\PROGRA~1\PuTTY\plink.exe -batch -i C:\Users\o51r15\.ssh\id_ed25519.ppk o51r15@192.168.1.192 "<command>"

# Copy file TO server
C:\PROGRA~1\PuTTY\pscp.exe -batch -i C:\Users\o51r15\.ssh\id_ed25519.ppk <local_file> o51r15@192.168.1.192:<remote_path>

# Copy file FROM server
C:\PROGRA~1\PuTTY\pscp.exe -batch -i C:\Users\o51r15\.ssh\id_ed25519.ppk o51r15@192.168.1.192:<remote_path> <local_file>
```

**PowerShell gotcha:** Special characters (`$`, single quotes inside double quotes, Python one-liners) get mangled by PowerShell. For complex commands, write a Python script locally, pscp it to `/tmp/` on the server, then execute it remotely with `python3 /tmp/script.py`.

### Docker Container Management

```bash
# Full rebuild and restart cycle (run on server via plink)
cd /home/o51r15/scripts/dockmon
docker build -t ghcr.io/o51r15/dockllama:dev .
docker stop dockllama && docker rm dockllama
docker run -d --name dockllama --restart unless-stopped \
  -p 8556:8556 \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /home/o51r15/docker/dockmon/config.yaml:/app/config/config.yaml \
  -v dockmon-data:/app/data \
  -e TZ=America/New_York \
  ghcr.io/o51r15/dockllama:dev

# Check logs
docker logs dockllama --tail 20

# Alternative: run from host (for debugging only)
cd ~/scripts/dockmon && nohup python3 -m dockllama config.yaml > /tmp/dockmon.log 2>&1 &
fuser -k 8556/tcp  # to stop host mode
```

### Git Workflow

All git operations run from `/home/o51r15/scripts/dockmon/` on the server:

```bash
git add <files>
git commit -m "descriptive message"
git push origin main
```

**config.yaml is gitignored.** Never push it. `config.example.yaml` is the git template.

---

## Project Structure

```
dockmon/
├── dockmon/
│   ├── __init__.py
│   ├── __main__.py          # Entry point (calls main.main())
│   ├── main.py              # Monitor loop, web server, digest scheduler, startup checks
│   ├── config.py            # Pydantic config schema + YAML loader
│   ├── db.py                # SQLite schema (events, cooldowns, baselines, digests, alert_urls)
│   ├── docker_client.py     # Docker SDK wrapper (get_client, get_logs, list_containers)
│   ├── log_pipeline.py      # Legacy pre-filter (ANSI strip, level detect, ignore patterns)
│   ├── log_analyzer.py      # v5 structured preprocessor (LogSummary with .to_prompt())
│   ├── ai_engine.py         # Ollama LLM evaluation (EvaluationContext → EvaluationResult)
│   ├── actions.py           # Restart/dry-run logic + cooldown system + compose group restarts
│   ├── alerts.py            # Apprise notification layer + DB persistence (load_alert_urls, save_alert_urls)
│   ├── digest.py            # Daily digest generation (gemma4) + DB storage
│   ├── trends.py            # 7d/30d health trend calculations
│   ├── prompts/
│   │   ├── v5_evaluate.txt  # Current eval prompt (structured input from log_analyzer)
│   │   └── v1_digest.txt    # Digest summary prompt
│   └── api/
│       ├── routes.py        # FastAPI REST endpoints (containers, logs, evaluate, events, alerts, digests, config, health)
│       └── events.py        # SSE event stream (publish/subscribe)
├── frontend/
│   ├── index.html           # Dashboard (container health grid only)
│   ├── insights.html        # Insights hub (sidebar: Health Trends, Recent Events, Log Explorer, Digest)
│   ├── settings.html        # Settings hub (sidebar: Notifications, Prompts)
│   ├── explorer.html        # Redirect → /insights.html?view=explorer
│   ├── digest.html          # Redirect → /insights.html?view=digest
│   └── prompts.html         # Redirect → /settings.html?view=prompts
├── config.yaml              # LOCAL config (gitignored) — 15 containers, real settings
├── config.example.yaml      # Generic template (in git)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .github/workflows/docker-publish.yml
└── .gitignore
```

---

## Evaluation Pipeline (How It Works)

Understanding this pipeline is critical for implementing Phases 7 and 8:

```
Raw Docker logs (200 lines)
    ↓
log_analyzer.py: analyze_logs()
    → Strips ANSI, Docker timestamps
    → Applies ignore_patterns (regex filter)
    → Counts severities (INFO/WARN/ERROR)
    → Detects restart sequences (shutdown → startup)
    → Deduplicates error messages with counts
    → Detects recovery (errors followed by clean lines)
    → Extracts recent tail (last 25 unfiltered lines)
    → Returns LogSummary with .to_prompt()
    ↓
ai_engine.py: evaluate()
    → Builds system prompt from v5_evaluate.txt
    → User prompt = LogSummary.to_prompt() output
    → Sends to Ollama (llama3.1:8b, format=json)
    → Parses EvaluationResult (status, health_score, confidence, etc.)
    ↓
main.py: _process_container()
    → Logs result
    → Stores in SQLite events table
    → Publishes via SSE
    → Executes action (restart/dry-run/none) based on result
    → Sends Apprise alerts
```

**Key files for Phase 7 (telemetry):** `docker_client.py` (add stats fetching), `log_analyzer.py` (add metrics to LogSummary), `v5_evaluate.txt` (add correlation rules)

**Key files for Phase 8 (context injection):** `config.py` (add context_prompt/examples/known_patterns to ContainerConfig), `ai_engine.py` (append context to system prompt), `log_analyzer.py` (metadata tagging), `main.py` (pass new fields through)

---

## Configuration

**config.yaml** (gitignored, on server): 15 containers, `poll_interval_seconds: 412`, `timeout_seconds: 300`, `base_url: "http://192.168.1.125:11434"`, `default_model: "llama3.1:8b"` (was briefly qwen2.5:7b-instruct, reverted) (was briefly qwen2.5:7b-instruct, reverted), `digest_model: "gemma4:latest"`, `dry_run: true`.

**Monitored containers (15):** gluetun, bitmagnet, bitmagnet-postgres, qbittorrent, kometa, audiobookshelf, pinchfork, pinchfork-db, jellyfin, karakeep, karakeep_chrome, karakeep_meilisearch, memos, tautulli, seerr

**Compose groups** (restart together): bitmagnet (bitmagnet + bitmagnet-postgres), pinchfork (pinchfork + pinchfork-db), karakeep (karakeep + karakeep_chrome + karakeep_meilisearch)

**Docker volume:** `dockmon-data:/app/data` persists the SQLite DB across container restarts.

---

## Database Schema

```sql
events (id, container, timestamp, event_type, ai_status, confidence,
        root_cause_category, summary, action_taken, log_snapshot,
        prompt_version, model_used, health_score)

cooldowns (container PK, last_restart, consecutive_restarts,
           current_cooldown_minutes, alert_only_mode)

baselines (container PK, healthy_log_sample, captured_at)

digests (id, date, generated_at, overall_health, headline,
         digest_json, formatted_text)

alert_urls (id, url UNIQUE, added_at)

container_prompts (container TEXT PK, context_prompt TEXT, examples TEXT,
                   known_patterns TEXT, updated_at TEXT)
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/containers | List all monitored containers + latest eval |
| GET | /api/containers/{name}/logs | Fetch container logs (raw + filtered) |
| POST | /api/containers/{name}/evaluate | On-demand AI evaluation |
| GET | /api/events | Paginated event history |
| GET | /api/events/{container}/restarts | Restart history for a container |
| GET | /api/trends | 7d/30d health trends |
| GET | /api/config | Current running config (sanitized) |
| GET | /api/health | System health check (Docker, Ollama, DB) |
| POST | /api/digest | Trigger on-demand digest |
| GET | /api/alerts | Get alert URLs (from DB) |
| PUT | /api/alerts | Update alert URLs (persisted to DB) |
| POST | /api/alerts/test | Send test notification |
| GET | /api/digests | List stored digests |
| GET | /api/digests/latest | Most recent digest |
| GET | /api/digests/{date} | Digest by date (YYYY-MM-DD) |
| GET | /api/containers/{name}/prompt | Get effective prompt config (DB + config fallback) |
| PUT | /api/containers/{name}/prompt | Save prompt config to DB |
| DELETE | /api/containers/{name}/prompt | Revert to config.yaml defaults |
| POST | /api/containers/{name}/test-prompt | Test prompt against current logs without saving |

---

## Key Architecture Decisions

### v5 Structured Preprocessor Pipeline

The biggest architectural win. Instead of dumping raw filtered logs to the LLM:

1. `log_analyzer.py` does all mechanical analysis in Python: timestamp parsing, severity counting, error deduplication, recovery detection, restart detection
2. Produces a `LogSummary` object with `.to_prompt()` method
3. LLM receives a structured summary (~20 lines) instead of raw logs (~200 lines)
4. Result: even 3B models get correct health assessments; 8B model is the sweet spot

### Auto-Healthy Fast Path

When ALL log lines match ignore patterns → `total_lines == 0` → skip LLM entirely → return synthetic healthy result (score=95). Prevents hallucination on empty input and saves compute.

### Model Selection

| Model | Size | Time | Use |
|-------|------|------|-----|
| llama3.1:8b | 4.7GB | ~11s | **Default** eval model |
| gemma4:latest | — | ~15s | Digest summaries (num_predict=4096) |

### Cold Model Problem

With poll intervals > 5 minutes, Ollama unloads the model from GPU. First eval hits cold model load (~30-40s). Fix: `timeout_seconds: 300` in config.

---

## Prompt Engineering History

Five versions, each fixing failure modes discovered live:

- **v1:** Basic 3-tier (healthy/unhealthy/critical). No root cause or restart reasoning.
- **v2:** Added error origin tracking, root cause categories, restart reasoning. Cut false restarts.
- **v3:** Shifted from "are there errors?" to "is the container doing its job?" PostgreSQL FATAL during planned shutdown = healthy.
- **v4:** Added "degraded" tier and 0–100 numeric score. Recency awareness with numbered log lines.
- **v5 (current):** Python preprocessor handles mechanical analysis; LLM only interprets structured summary. Fixed the fundamental pipeline flaw where filtered INFO lines removed recovery evidence.

---

## CI/CD

`.github/workflows/docker-publish.yml`: push to main → `ghcr.io/o51r15/dockllama:dev`, GitHub Release → `:latest` + semver. Currently running `:dev` built locally (GHCR pull had timeout issues).

---

## Development Timeline

### Session 1 — Initial Build (July 17–18, 2026)

Built entire project from scratch. Phases 0–5: scaffolding → log pipeline/AI engine → actions/cooldowns/alerts → FastAPI/SSE/dashboard → digest/scheduler → hardening. 8 containers. Prompt evolution v1–v4. Discovered the recency problem.

### Session 2 — Preprocessor & Scale (July 18, 2026)

Built v5 structured preprocessor (biggest quality improvement). Switched to llama3.1:8b. Added GitHub Actions CI/CD. Expanded to 15 containers. Separated config.

### Session 3 — Features & Fixes (July 19–23, 2026)

Auto-healthy fast path. Expanded Chrome ignore patterns. Settings page + Digest viewer. Fixed digest generation (num_predict too low for 15 containers + robust JSON extraction). Persisted notification URLs in SQLite. Updated roadmap with Phases 7–11 including detailed telemetry and context injection plans.

Key commits this session: `d3b4c52` (auto-healthy, digest fix, settings/digest pages), `fce7554` (digest num_predict bump), `6bee8b0` (alert URL persistence fix).
### Session 4 — Telemetry, Context Injection & Model Tuning (July 23, 2026)

**Rename:** Completed dockmon → dockllama across 32 files (package dir, imports, Docker, CI/CD, compose, strings).

**Phase 7 (Telemetry):** Added `get_container_stats()` to docker_client.py using Docker stats API with delta CPU calculation. Added cpu_percent/mem_percent to LogSummary dataclass and to_prompt() output. Added resource correlation rules to v5_evaluate.txt. All 15 containers now report CPU/RAM metrics.

**Phase 8.1-8.3 (Context Injection):** Added context_prompt, examples, and known_patterns fields to ContainerConfig. context_prompt injects container-specific knowledge into the system prompt. Few-shot examples provide calibration scenarios. known_patterns tag matching log lines with [ROUTINE:] metadata. Configured context for gluetun (port forwarding failures), bitmagnet-postgres (shutdown sequences), and karakeep_chrome (headless Chromium noise).

**Base prompt improvements:** Added "What you are reading" section to v5_evaluate.txt explaining the full preprocessing pipeline (ignore_patterns, known_patterns, severity counting, context overrides). Added "Reading the severity line" section explaining routine counts.

**Routine counts:** Added routine_counts tracking to log_analyzer.py — counts how many ERROR/WARN lines carry [ROUTINE:] tags and displays inline: `18 (18 routine) ERROR`.

**Model switch:** Switched from qwen2.5:7b-instruct back to llama3.1:8b for better context-following.

**Known bug:** karakeep_chrome scores 60 (DEGRADED) despite all errors being routine Chromium noise. See BUG_karakeep_chrome_scoring.md for full analysis and future fix ideas. Best candidate: reclassify routine-tagged lines as INFO in the preprocessor.

Key commits: rename (32 files), Phase 7 metrics pipeline, Phase 8 context injection, preprocessing explanation in base prompt, routine counts in severity display.


### Session 5-6 — Model Testing, Stats History, Bug Fixes & Config Persistence (July 23, 2026)

**Phase 9.1-9.3 (Model Validation & Scheduling):** Model discovery UI queries Ollama for available models. Validation testing sends healthy/failing test fixtures with warmup call to avoid cold-start benchmark skew. Results persist in tested_models DB table with full results_json. Selectable model cards show stored test results and interval calc. Hardware-aware interval slider with red/yellow/green zones auto-jumps to recommended value.

**Phase 7B (Stats History & Resource Charts):** Container stats persist to container_stats table with configurable retention. API endpoints with downsampling for 1h/24h/7d/30d ranges. Dashboard sparklines on each card. Detail drawer with Chart.js line charts. Fleet overview bar.

**DB path fix:** Config had db_path pointing to host path inaccessible inside container. Fixed to /app/data/dockllama.db (volume-mounted). Migrated old data (3772 events, 16 baselines, 5 digests). Added warning comment to config.yaml.

**Evaluate Now bug fixes:** Button click bubbled to detail drawer (added @click.stop). On-demand eval now saves to events table as on_demand_eval event type so dashboard reflects new results.

**Config persistence:** Interval and model changes from UI now persist to config.yaml via targeted regex replacement (preserves comments/formatting). Removed :ro from config mount. Single source of truth.

Key commits: ad8df22, 851c8bc, 487f205, 98c4eb9, 7487933.

---

## Lessons Learned

- **Never send empty data to an LLM.** If nothing to evaluate, use auto-healthy fast path.
- **Filter Chrome by source filename, not message text.** Source filenames are stable across versions.
- **Make every LLM JSON field optional with defaults.** llama3.1:8b occasionally omits fields.
- **Set num_predict high enough for output size.** gemma4 digest for 15 containers needs ~3000 tokens.
- **Account for cold model load time.** Set timeout_seconds: 300.
- **PowerShell mangles special chars.** Write Python scripts, pscp to server, execute remotely.
- **Separate local config from git.** config.yaml is gitignored.

---

## Known Issues

1. **karakeep_chrome scores DEGRADED (60) despite being healthy** — OPEN BUG

   **Problem:** karakeep_chrome is a headless Chromium container. It logs 18 ERROR and 15 WARN lines at every startup — all normal artifacts of running without D-Bus, Bluetooth, audio, or a desktop. After ignore_pattern filtering, 37 lines survive with 0 INFO, 15 WARN, 18 ERROR. The LLM can't score this healthy because it sees 100% error/warning content.

   **What we tried (in order):**
   - Heavy ignore_patterns (22 patterns → auto-healthy): Worked but user rejected — "teach the AI, don't hide data"
   - Rolled back to 1 ignore_pattern (config_dir_policy_loader spam only)
   - context_prompt explaining every error type is normal + explicit scoring rules: LLM acknowledges context but still scores low
   - Few-shot example (normal Chromium startup scored 95/healthy): Helps but doesn't override severity counts
   - known_patterns with [ROUTINE:] tags on all 4 major error types: Tags visible but LLM still weighs raw counts
   - Switched qwen2.5:7b-instruct → llama3.1:8b: Improved 35→60 but still not 80+
   - Added preprocessing explanation to base prompt (v5_evaluate.txt): Marginal improvement
   - Added routine_counts to severity display (`18 (18 routine) ERROR`): LLM sees 0 real errors mathematically but still anchors on the raw count

   **Root cause:** Small LLMs (7-8B) have strong priors that errors=bad. When the severity header shows `18 ERROR`, no amount of in-context instruction fully overrides that anchor — especially when there are 0 INFO lines to provide a "healthy" signal.

   **Best fix candidates (for future sessions):**
   1. **Reclassify routine-tagged lines as INFO in preprocessor** — after tagging `[ROUTINE:]`, change the line's severity from ERROR→INFO. Counts become `33 INFO, 0 WARN, 0 ERROR`. Original severity preserved in line text. One-line change, stays true to "teach don't hide."
   2. **Extend auto-healthy fast path** — trigger when ALL error/warn lines are tagged ROUTINE (not just when all lines match ignore_patterns). Skip LLM entirely.
   3. **Larger model** — 13B+ models may handle context override better.

   **Relevant config (as of Session 4):**
   - 1 ignore_pattern: `config_dir_policy_loader`
   - Full context_prompt explaining all Chromium noise
   - 1 few-shot example (normal startup → 95/healthy)
   - 4 known_patterns with [ROUTINE:] tags (D-Bus, Bluetooth, sandbox, bluez)
2. **Docker image tag mismatch** — docker-compose.yml references `:latest` but active container runs `:dev`. Next GitHub Release will sync.
3. **Docker volume vs host DB** — RESOLVED. — container uses `dockmon-data:/app/data`. Host path `/home/o51r15/scripts/dockmon/data/dockllama.db` is separate. Don't confuse them.
4. **Evaluate Now doesn't update dashboard** — RESOLVED (Session 6). On-demand eval saves to events table but fetchContainers() still shows stale data. The /api/containers endpoint may return the previous eval cycle's cached result rather than the freshly inserted event. Needs investigation into whether the containers endpoint queries the latest event or uses an in-memory cache.
5. **Sparkline flicker causes layout shift** — RESOLVED (Session 6). CPU/RAM sparklines on dashboard cards disappear and reappear every few seconds, causing the entire grid to shift up and down. Likely caused by loadSparklines() clearing c._spark (triggering x-show hide) before the async fetch completes with new data. Fix: set new data atomically, or reserve sparkline height with CSS min-height so layout does not shift during loading.
6. **Ollama error response not handled — eval failures under GPU contention** — OPEN BUG. When another process (video transcoder) uses the GPU, Ollama returns HTTP 200 with `{"error": "Connection refused"}`. `ai_engine.py` doesn't check for the `error` key — tries to parse `data["response"]` which doesn't exist, hits JSONDecodeError, retries (same failure), returns fallback score 50 / 0% confidence. Fix: check `if "error" in data:` before parsing, add retry with backoff.
7. **`_update_yaml_field()` regex backreference injection** — OPEN BUG. In `config.py`, `pattern.subn(r"\g<1>" + val_str, text)` interprets backslash sequences in val_str as regex backreferences. Fix: use lambda replacement.
8. **`save_containers_to_config()` destroys YAML comments** — OPEN BUG. Uses `yaml.safe_load()` → modify → `yaml.dump()` which strips all comments including DB path warning. Fix: use ruamel.yaml or targeted text replacement.
9. **`_config_path` Pydantic v2 private field** — MINOR. Should use `PrivateAttr()` instead of plain annotation. Works by accident.
10. **DELETE body for container removal non-standard** — MINOR. Some proxies strip DELETE request bodies. Consider query param `?purge=true`.
11. **Dead code in `addContainer()`** — MINOR. `dc._adding = false` after `loadDockerContainers()` references stale object.
