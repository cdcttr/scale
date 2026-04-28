# Symphony — Python Implementation Design

**Date:** 2026-04-28
**Status:** Approved
**Stack:** Python · asyncio · FastAPI · Rich · GitHub Issues · Claude Code

---

## Overview

Symphony is a long-running daemon that turns a GitHub Issues board into an always-on coding-agent orchestrator. It polls for open issues, spins up an isolated workspace per issue, launches a `claude` CLI subprocess to work on it autonomously, and manages retries, stall detection, and reconciliation. Engineers manage work (issues) rather than supervising individual agent sessions.

This implementation follows the [Symphony SPEC.md](https://github.com/openai/symphony/blob/main/SPEC.md) fully, replacing:
- **Linear** → GitHub Issues (tracker)
- **Codex app-server** → `claude` CLI (agent runtime)

---

## Architecture

A single Python process runs one `asyncio` event loop. The Orchestrator owns the poll-and-dispatch cycle and all shared state; all other components are dependencies it calls into. The FastAPI HTTP server runs as a background asyncio task in the same process, sharing orchestrator state directly with no IPC.

```
┌─────────────────────────────────────────────────────┐
│                    Symphony Daemon                   │
│                                                      │
│  ┌──────────────┐     ┌────────────────────────┐    │
│  │ Config Loader │────▶│      Orchestrator       │    │
│  │ (watchfiles) │     │  poll loop · state map  │    │
│  └──────────────┘     │  retry queue · claims   │    │
│                       └──────┬─────────┬─────────┘   │
│  ┌──────────────┐            │         │              │
│  │  GitHub      │◀───────────┘         │              │
│  │  Tracker     │                      │              │
│  │  Client      │               ┌──────▼──────┐      │
│  └──────────────┘               │ Worker Pool  │      │
│                                 │ local / SSH  │      │
│  ┌──────────────┐               └──────┬───────┘      │
│  │  FastAPI     │                      │              │
│  │  HTTP API    │               ┌──────▼───────┐      │
│  └──────────────┘               │ Workspace Mgr │      │
│                                 └──────┬────────┘     │
│  ┌──────────────┐                      │              │
│  │  Rich        │               ┌──────▼───────┐      │
│  │  Dashboard   │               │ Claude Runner │      │
│  └──────────────┘               └──────────────┘      │
└─────────────────────────────────────────────────────┘
```

### Package Layout

```
symphony/
├── symphony/
│   ├── main.py          # CLI entry point (argparse)
│   ├── config/          # WORKFLOW.md loader + Pydantic schema
│   ├── tracker/         # Abstract base + GitHub implementation
│   ├── orchestrator/    # Polling loop, state machine, dispatch
│   ├── workspace/       # Per-issue dirs, lifecycle hooks
│   ├── worker/          # local.py + ssh.py
│   ├── agent/           # Abstract base + claude.py runner
│   ├── prompt/          # Liquid template renderer
│   ├── api/             # FastAPI app + /api/v1/* routes
│   └── dashboard/       # Rich live TUI
├── WORKFLOW.md.example
└── pyproject.toml
```

### Key Dependencies

| Package | Purpose |
|---|---|
| `asyncio` | Concurrency model throughout |
| `pydantic v2` | Config validation and domain models |
| `python-frontmatter` | YAML front matter parsing from WORKFLOW.md |
| `python-liquid` | Strict Liquid-compatible prompt template rendering |
| `httpx` | Async GitHub REST API calls |
| `fastapi` + `uvicorn` | HTTP API server |
| `rich` | Live TUI dashboard |
| `watchfiles` | WORKFLOW.md hot-reload |

---

## GitHub Issues Integration & State Model

### Tracker Config (`WORKFLOW.md`)

```yaml
tracker:
  kind: github
  repo: owner/repo            # replaces Linear project_slug
  api_token: $GITHUB_TOKEN
  active_labels: []           # empty = any open issue is active
  skip_labels:                # open issues with these labels are ignored
    - symphony:skip
    - wontfix
  terminal_labels:            # these + closed = terminal (triggers cleanup)
    - symphony:done
```

### State Resolution (priority order)

1. Issue is **closed** → terminal
2. Issue has a **terminal label** → terminal
3. Issue has a **skip label** → ignored (not dispatched, not cleaned up)
4. Issue is **open** + has all required `active_labels` (if configured) → active
5. Otherwise → ignored

### GitHub API Operations

| Operation | Endpoint |
|---|---|
| `fetch_candidate_issues()` | `GET /repos/{owner}/{repo}/issues?state=open&per_page=100` (filtered client-side) |
| `fetch_issues_by_ids(ids)` | `GET /repos/{owner}/{repo}/issues/{number}` (parallelized with `asyncio.gather`) |
| `fetch_terminal_issues()` | `GET /repos/{owner}/{repo}/issues?state=closed&per_page=100` + open issues with terminal labels |

### Normalized `Issue` Domain Model

```python
@dataclass
class Issue:
    id: str           # GitHub node ID
    identifier: str   # "owner/repo#123"  — used for workspace dir name
    number: int       # 42
    title: str
    description: str  # issue body
    state: str        # "active" | "terminal" | "ignored"
    labels: list[str]
    branch_name: str  # auto-derived: "symphony/42-slug-of-title"
    url: str
    priority: int | None   # set via label "priority:1" etc., else None
    created_at: datetime
    updated_at: datetime
```

### Priority Sorting

Issues dispatched in this order:
1. `priority:1` label (highest) → `priority:4` (lowest) → unlabeled (lowest)
2. Then `created_at` ascending (oldest first)
3. Then issue number ascending

---

## Claude Code Runner

### Turn Invocation

```bash
# First turn — full issue prompt
claude --print \
       --output-format stream-json \
       --dangerously-skip-permissions \
       --max-turns 1 \
       -p "<rendered issue prompt>"

# Continuation turn — narrowed guidance
claude --print \
       --output-format stream-json \
       --dangerously-skip-permissions \
       --continue \
       --max-turns 1 \
       -p "<continuation prompt>"
```

- `--output-format stream-json`: structured newline-delimited JSON events for token counting and stall detection
- `--dangerously-skip-permissions`: auto-approves all tool calls. **Approval policy is `auto`. This must be understood and accepted by the operator before deployment.**
- `--continue`: reuses the most recent session in the workspace directory — multi-turn continuity without a persistent session ID
- `--max-turns 1`: Symphony's `agent.max_turns` config governs the outer loop; Claude does not loop internally

### Stream Event Handling

| Event type | Action |
|---|---|
| `assistant` message | Update last-event timestamp, count tokens |
| `tool_use` | Update timestamp, log tool name |
| `result` with `subtype: success` | Turn succeeded — schedule continuation or finish |
| `result` with `subtype: error` | Turn failed — schedule backoff retry |
| Process exits non-zero | Abnormal exit — backoff retry |
| No event for `stall_timeout_ms` | Orchestrator kills PID, schedules retry |

### Token Accounting

The `result` event contains `usage.input_tokens` and `usage.output_tokens`. Symphony tracks these as absolute totals per session (not deltas) and accumulates into the orchestrator's aggregate counters.

### Optional `github_rest` Client-Side Tool

Symphony embeds a minimal MCP server that advertises a `github_rest` tool to Claude. Claude can use it to read its own issue, post status comments, or fetch related issues — all authenticated via Symphony's `GITHUB_TOKEN`, scoped to the configured repo only.

---

## WORKFLOW.md Schema

Front matter is YAML; the body is the Liquid prompt template.

```yaml
---
tracker:
  kind: github
  repo: owner/repo
  api_token: $GITHUB_TOKEN
  active_labels: []
  skip_labels: [symphony:skip, wontfix]
  terminal_labels: [symphony:done]

polling:
  interval_ms: 30000

workspace:
  root: ./workspaces          # relative to this file; ~ and $VAR supported

hooks:
  after_create: git clone https://github.com/owner/repo .
  before_run: git fetch && git checkout -B symphony/{{ issue.number }}
  after_run: ""
  before_remove: ""
  timeout_ms: 60000

agent:
  max_concurrent_agents: 10
  max_turns: 20
  max_retry_backoff_ms: 300000
  max_concurrent_agents_by_state: {}

codex:                        # key kept for spec compatibility; controls claude CLI
  command: claude
  approval_policy: auto       # only supported value
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000

server:
  port: 8080                  # omit to disable HTTP API

worker:                       # optional SSH extension
  ssh_hosts:
    - user@host1.example.com
    - user@host2.example.com
  max_concurrent_agents_per_host: 3
---

You are working on GitHub issue {{ issue.identifier }}: **{{ issue.title }}**.

## Task
{{ issue.description }}

## Context
- Branch: `{{ issue.branch_name }}`
- Labels: {{ issue.labels | join: ", " }}
- URL: {{ issue.url }}
{% if attempt %}
## Retry context
This is attempt {{ attempt }}. Review any previous work in the workspace and continue from where it left off.
{% endif %}

## Instructions
1. Implement the changes described above in this repository
2. Write or update tests as needed
3. Ensure CI would pass
4. Open a pull request when done
```

### Dynamic Reload

`watchfiles` watches WORKFLOW.md. On change, Pydantic re-validates the new config. If valid, the orchestrator swaps it in live — no restart needed. If invalid, it logs the error and keeps the last good config.

### `$VAR` Resolution

Any string value starting with `$` is replaced with `os.environ[VAR]` at load time. Missing env vars are a hard startup error.

---

## Orchestrator State Machine

### In-Memory State

```python
@dataclass
class OrchestratorState:
    running:       dict[str, LiveSession]   # issue_id → active session
    claimed:       set[str]                 # reserved, prevents double-dispatch
    retry_queue:   list[RetryEntry]         # sorted by due_at
    completed:     set[str]                 # terminal cleanup done
    token_totals:  TokenTotals              # aggregate input/output across all runs
```

All mutations go through a single `asyncio.Lock`.

### Poll-and-Dispatch Tick (every `interval_ms`)

```
1. Reconcile running sessions
   ├── stall check: elapsed > stall_timeout_ms → kill + backoff retry
   └── state refresh from GitHub
       ├── terminal → kill + clean workspace
       ├── still active → update snapshot
       └── neither → kill, no cleanup

2. Validate config (skip dispatch if invalid, keep reconciling)

3. Fetch candidate issues from GitHub

4. Sort by priority → created_at → number

5. Dispatch eligible issues until slots exhausted
   Eligibility: active state, not claimed/running,
   global concurrency ok, per-state concurrency ok,
   no unresolved blockers (issues that block this one still open)

6. Fire pending retry timers whose due_at has passed
```

### Worker Lifecycle (one `asyncio.Task` per issue)

```
create/reuse workspace
  → before_run hook
    → spawn claude subprocess
      → stream turns (up to max_turns)
        → after each turn: re-check issue state from GitHub
      → stop session
    → after_run hook (best-effort)
  → exit normally  → continuation retry after 1 000ms
    exit with error → exponential backoff retry
```

### Retry Backoff

- **Continuation** (normal exit): 1 000ms fixed
- **Failure** (abnormal): `min(10_000 × 2^(attempt−1), max_retry_backoff_ms)`

### Startup Terminal Cleanup

On startup, Symphony fetches all terminal-state issues from GitHub and removes their workspace directories before beginning the polling loop.

---

## HTTP API

FastAPI server runs as a background `asyncio` task in the same process.

### Endpoints

```
GET  /api/v1/state              Full system snapshot
GET  /api/v1/<issue_identifier> Per-issue detail
POST /api/v1/refresh            Queue immediate poll + reconcile
```

### Example `GET /api/v1/state`

```json
{
  "running": [
    {
      "issue_identifier": "owner/repo#42",
      "title": "Add dark mode",
      "session_id": "abc123-turn-3",
      "turn_count": 3,
      "tokens": { "input": 12400, "output": 3100 },
      "started_at": "2026-04-28T13:00:00Z",
      "last_event_at": "2026-04-28T13:04:12Z"
    }
  ],
  "retrying": [
    {
      "issue_identifier": "owner/repo#17",
      "attempt": 2,
      "due_at": "2026-04-28T13:06:00Z",
      "error": "claude exited with code 1"
    }
  ],
  "token_totals": { "input": 84200, "output": 21300, "total": 105500 },
  "agent_count": { "running": 1, "retrying": 1, "completed": 7 }
}
```

SSH hosts field (when SSH workers are enabled):
```json
"hosts": [
  { "host": "user@host1.example.com", "running": 2, "capacity": 3 },
  { "host": "user@host2.example.com", "running": 3, "capacity": 3 }
]
```

---

## Rich TUI Dashboard

Enabled by default when stdout is a TTY; switches to plain structured logs automatically when piped, in CI, or under systemd/Docker.

```
Symphony  ●  4 running  2 retrying  12 completed          [04/28 13:04:22]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  RUNNING
  #42  Add dark mode              turn 3/20   12.4k in  3.1k out   4m 12s
  #51  Fix pagination bug         turn 1/20    2.1k in    800 out      42s
  #63  Upgrade deps               turn 7/20   31.2k in  8.4k out  18m 05s
  #71  Write auth tests           turn 2/20    5.6k in  1.2k out   2m 18s

  RETRYING
  #17  Refactor user model        attempt 2   retry in 1m 32s  (exit code 1)
  #29  Add CSV export             attempt 1   retry in 0m 08s  (stall timeout)

  TOTALS  105.5k tokens  •  7 completed  •  uptime 2h 14m
```

Refreshes every 2 seconds using `rich.live.Live`.

---

## SSH Worker Extension

The orchestrator stays local; `claude` subprocesses run on remote hosts over SSH stdio.

### Execution

`ssh.py` implements the same worker interface as `local.py`. Instead of a local subprocess it runs:

```bash
ssh -T user@host1.example.com "bash -lc 'claude --print --output-format stream-json ...'"
```

Workspace directories live on the remote host. Hooks also execute remotely via SSH.

### Scheduling

- Orchestrator maintains a per-host slot counter alongside the global counter
- Dispatch prefers the host that last ran the same issue (workspace locality on retry)
- If preferred host is full, fall back to any host with capacity
- If all hosts saturated: requeue with a "no slots" retry (no backoff penalty, retries quickly)
- Unreachable hosts at startup: logged as warning, excluded from pool, remaining hosts continue

### Safety

- Workspace path safety check (prefix within `workspace.root`) is enforced via hook scripts on the remote host
- SSH host strings are validated at config load time (format `[user@]host`)
- `after_run` hook failure on a remote host is logged and ignored, same as local

---

## Error Handling

| Failure class | Behavior |
|---|---|
| Invalid WORKFLOW.md | Keep last good config, log error, skip dispatch |
| Workspace creation failure | Abort attempt, backoff retry |
| `before_run` hook failure | Abort attempt, backoff retry |
| `after_run` hook failure | Log warning, ignore |
| Claude subprocess non-zero exit | Backoff retry |
| Stall timeout | Kill PID, backoff retry |
| GitHub API transport error (candidate fetch) | Skip tick, try next interval |
| GitHub API transport error (state refresh) | Keep workers running, retry next tick |
| HTTP API / dashboard error | Log, don't crash orchestrator |
| SSH host unreachable at startup | Exclude host, warn, continue |

---

## Security Notes

- `--dangerously-skip-permissions` is required for unattended operation. Operators must understand this grants Claude full tool-call approval. Tighten by limiting available tools via Claude Code settings.
- `GITHUB_TOKEN` must have minimum required scopes (`repo` for private, `public_repo` for public).
- Workspace directories are sanitized (`[^A-Za-z0-9._-]` → `_`) and path-prefix-checked before any agent is launched.
- SSH hosts are fully trusted; use dedicated low-privilege users and restrict `~/.ssh/authorized_keys`.
- The embedded MCP server scopes `github_rest` calls to the configured repo only.

---

## Implementation Checklist

### Core
- [ ] `config/` — WORKFLOW.md loader, Pydantic schema, `$VAR` resolution, hot-reload
- [ ] `tracker/github.py` — candidate fetch, state refresh, terminal fetch, normalized Issue model
- [ ] `orchestrator/` — poll loop, state machine, dispatch eligibility, concurrency control
- [ ] `workspace/manager.py` — create/reuse, sanitization, path safety, lifecycle hooks
- [ ] `agent/claude.py` — subprocess launch, stream-json parsing, token accounting
- [ ] `worker/local.py` — asyncio subprocess wrapper
- [ ] `prompt/renderer.py` — strict Liquid renderer, fail on unknown vars
- [ ] Retry logic — continuation (1s) + exponential backoff
- [ ] Reconciliation — stall detection, state refresh, terminal cleanup
- [ ] Structured logging with `issue_id`, `issue_identifier` context

### Extensions
- [ ] `api/` — FastAPI server, `/api/v1/state`, `/api/v1/<id>`, `/api/v1/refresh`
- [ ] `dashboard/` — Rich live TUI, auto-detect TTY
- [ ] `worker/ssh.py` — SSH worker, per-host slot tracking, locality-aware scheduling
- [ ] Optional `github_rest` MCP tool embedded in daemon
- [ ] Startup terminal workspace cleanup
