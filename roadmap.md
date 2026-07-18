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

## Next Major Feature: Learning Mode

The core problem with generic AI healthchecks is that every container has its own definition of "normal." Gluetun logging port forwarding errors might be a critical failure or routine noise depending on the user's setup. The AI can't know without being taught.

Learning Mode lets Dockmon observe containers passively, surface patterns it thinks might be problems, and learn from user feedback what actually matters for each container.

### How It Works

**1. Observation Phase**

When a container is set to `mode: learning`, Dockmon watches it normally but takes NO action and makes NO health judgment. Instead, it runs a different LLM prompt focused on pattern detection:

- Identify recurring log patterns (group similar lines by structure, not content)
- Flag patterns that MIGHT indicate problems (errors, warnings, timeouts, connection failures)
- Track frequency and timing of each pattern (constant? bursty? correlated with other events?)
- Note patterns that appear during known-good operation vs. patterns that are new

Each monitoring cycle produces a list of "candidate patterns" stored in a new DB table.

**2. Pattern Presentation**

The dashboard gets a Learning Mode view per container showing:

- Detected patterns grouped by category (network errors, timeouts, process warnings, etc.)
- Example log lines for each pattern
- Frequency data (how often, when, trending up/down?)
- The AI's initial guess: "This looks like it could be a problem because..." or "This looks normal because..."

The user reviews each pattern and marks it:
- **Problem** -- "Yes, this indicates a real failure. Flag this."
- **Normal** -- "No, this is expected. Ignore this."
- **Watch** -- "I'm not sure yet. Keep tracking it but don't act on it."

**3. Rule Generation**

User decisions become per-container rules stored in the DB:

```yaml
# Auto-generated from learning mode feedback
container_rules:
  gluetun:
    failure_patterns:
      - pattern: "port forwarding.*connection refused"
        severity: warning
        description: "Port forwarding failed -- services behind VPN lose inbound connectivity"
        learned_from: "user marked as problem on 2026-07-20"
    normal_patterns:
      - pattern: "Shutdown failed.*goroutine shutdown timed out"
        description: "Normal during container restart"
        learned_from: "user marked as normal on 2026-07-20"
  bitmagnet:
    normal_patterns:
      - pattern: "search failed.*API returned status 400"
        description: "Some indexers are frequently offline, this is expected"
      - pattern: "context deadline exceeded.*prowlarr"
        description: "Prowlarr indexer timeouts are transient and normal"
```

**4. Graduated Promotion**

Once the user has reviewed enough patterns (configurable threshold, e.g. 10+ patterns marked), the container can be promoted from `learning` to `monitoring` mode. The learned rules are injected into the evaluation prompt as container-specific context:

```
## Container-Specific Rules (learned)

The user has confirmed these patterns for this container:
KNOWN PROBLEMS (flag as unhealthy):
- "port forwarding.*connection refused" -- Port forwarding failure, causes inbound connectivity loss
KNOWN NORMAL (ignore):
- "goroutine shutdown timed out" -- Expected during restart
```

This gives the LLM the domain knowledge it was missing, without requiring the user to write regex rules by hand.

### Implementation Plan

**Phase L1 -- Pattern Detection Engine**
- New LLM prompt: `v1_learn.txt` -- "Identify recurring patterns in these logs. For each pattern, describe what it looks like, how often it occurs, and whether you think it could indicate a problem."
- New DB table: `learned_patterns (id, container, pattern_regex, pattern_description, example_lines, frequency, ai_assessment, user_verdict, created_at, updated_at)`
- New DB table: `container_rules (id, container, rule_type, pattern, severity, description, source, created_at)`
- Pattern deduplication logic -- cluster similar log lines into patterns using the LLM
- Store pattern frequency and first/last seen timestamps

**Phase L2 -- Learning Mode Dashboard**
- New frontend page: `/learning.html`
- Per-container pattern list with example logs, frequency, AI guess
- Approve/reject/watch buttons for each pattern
- API endpoints:
  - `GET /api/learning/{container}/patterns` -- list detected patterns
  - `POST /api/learning/{container}/patterns/{id}/verdict` -- user marks problem/normal/watch
  - `GET /api/learning/{container}/rules` -- view generated rules
  - `POST /api/learning/{container}/promote` -- graduate to monitoring mode

**Phase L3 -- Rule Injection into Evaluation**
- When evaluating a container with learned rules, append them to the evaluation prompt
- Rules act as few-shot examples: "The user has told you that X is normal and Y is a problem"
- Track whether learned rules improve or change evaluation accuracy over time
- Allow users to edit/delete rules from the dashboard

**Phase L4 -- Continuous Learning**
- Even in monitoring mode, flag NEW patterns not seen during learning
- Present new patterns to the user periodically: "I noticed a new pattern in gluetun I haven't seen before..."
- Allow re-entering learning mode to refine rules
- Export/import rules between containers with similar roles

### Config Changes

```yaml
containers:
  - name: "gluetun"
    enabled: true
    mode: "learning"          # "learning" | "monitoring" (default: monitoring)
    learning:
      min_patterns_before_promote: 10
      observation_cycles: 100  # how many cycles to observe before presenting patterns
    rules_file: null           # auto-generated, or path to manual rules yaml
```

### Key Design Decisions

- Learning mode uses a SEPARATE prompt from evaluation -- it's not trying to judge health, just identify patterns
- Pattern detection runs through the LLM, not regex -- the AI clusters "connection refused to 10.2.0.1:5351" and "connection refused to 10.2.0.1:5352" as the same pattern
- User verdicts are stored permanently and survive prompt version upgrades
- Rules are human-readable YAML, editable by hand if needed
- A container can be in learning mode indefinitely -- no pressure to promote

## Other Future Features

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
