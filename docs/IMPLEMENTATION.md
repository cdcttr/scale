# Symphony — Implementation Reference

**Version:** 0.1.0
**Stack:** Python 3.12 · asyncio · FastAPI · Rich · GitHub Issues · Claude Code CLI
**Entry point:** `symphony/main.py` → `symphony.main:main`

---

## 1. Overview

Symphony is a long-running Python daemon that turns a GitHub Issues board into an always-on coding-agent orchestration system. Instead of engineers supervising individual Claude Code sessions, Symphony watches the issue tracker, claims open issues, provisions isolated workspaces, and autonomously drives the `claude` CLI to completion — retrying on failure, rotating through issues as concurrency slots free up, and keeping a live dashboard of everything in flight.

The problem it solves is simple: Claude Code is powerful but manual. Symphony removes the human from the loop. An engineer opens a GitHub issue describing a coding task; Symphony picks it up, runs Claude Code inside a cloned repository, loops until a pull request is opened or the turn limit is reached, and then moves on to the next issue. If an agent stalls or crashes, Symphony retries it with exponential backoff. When an issue is closed or labelled `symphony:done`, Symphony removes the workspace and stops retrying.

The design follows the OpenAI Symphony orchestration spec, substituting GitHub Issues for Linear and the `claude` CLI for Codex's app-server. The result is a self-contained Python process with no external orchestration dependencies.

---

## 2. Architecture

### 2.1 Process Model

Symphony runs as a single Python process with a single `asyncio` event loop. Every long-running activity — the poll loop, the HTTP API server, the dashboard, the file watcher, and every per-issue agent — runs as an `asyncio.Task` within that one process. There is no IPC, no message queue, and no external scheduler.

The `Orchestrator` is the central object. It owns all shared in-memory state and drives the poll-and-dispatch cycle. Every other component is a dependency the Orchestrator calls into. The FastAPI server and Rich dashboard both read from the same `OrchestratorState` object that the Orchestrator writes to, without any synchronization overhead beyond an `asyncio.Lock` for mutation.

### 2.2 Component Map

```
symphony/main.py          — CLI entry point; wires components; starts asyncio.gather
symphony/config/
  schema.py               — Pydantic v2 models for all WORKFLOW.md sections
  loader.py               — frontmatter parse, $VAR resolution, path normalization
  watcher.py              — watchfiles hot-reload loop
symphony/tracker/
  models.py               — Issue dataclass (normalized domain model)
  base.py                 — TrackerClient abstract base
  github.py               — GitHub REST API client
symphony/orchestrator/
  state.py                — OrchestratorState, LiveSession, RetryEntry, TokenTotals
  dispatch.py             — is_eligible(), sort_issues(), retry_delay_ms()
  core.py                 — Orchestrator: poll loop, dispatch, reconcile, retry
symphony/workspace/
  manager.py              — WorkspaceManager: per-issue dirs, hooks, path safety
symphony/agent/
  claude.py               — ClaudeRunner: subprocess, stream-json parsing, token accounting
symphony/worker/
  base.py                 — Worker abstract base
  local.py                — LocalWorker: multi-turn loop, continuation prompts
  ssh.py                  — SSHWorker: remote execution over SSH stdio
symphony/prompt/
  renderer.py             — Liquid template renderer (strict undefined)
symphony/api/
  server.py               — FastAPI app factory
  routes.py               — /api/v1/* route handlers
symphony/dashboard/
  ui.py                   — Rich Live TUI
```

### 2.3 Data Flow

```
GitHub Issues
     │
     │  fetch_candidate_issues() every interval_ms
     ▼
Orchestrator._tick()
     │
     ├─ _reconcile()         ← stall checks, state refresh, terminal cleanup
     ├─ _fire_retries()      ← promote due RetryEntry objects back into dispatch
     │
     ├─ sort_issues()        ← priority → created_at → number
     │
     └─ for each eligible issue:
           asyncio.create_task(_run_worker(issue, attempt))
                │
                ├─ WorkspaceManager.prepare()     ← mkdir, after_create hook
                ├─ WorkspaceManager.run_before_hook()
                │
                ├─ for turn 0..max_turns:
                │     render_prompt() → ClaudeRunner.run_turn()
                │          └─ claude subprocess → stream-json lines
                │                └─ parse_stream_event() → TurnResult
                │
                ├─ WorkspaceManager.run_after_hook()
                │
                ├─ success → _schedule_retry(attempt=None, delay=1s)  [continuation]
                └─ failure → _schedule_retry(attempt=N, exponential backoff)
```

The HTTP API and TUI read `OrchestratorState` directly; they never write to it.

---

## 3. Configuration

### 3.1 WORKFLOW.md Structure

Symphony's entire configuration lives in a single file named `WORKFLOW.md`. The file uses YAML front matter (the block between `---` delimiters) for structured configuration and the Markdown body as a Liquid prompt template. The `python-frontmatter` library splits these two sections at load time.

```
---
<YAML front matter — structured config>
---

<Liquid template — sent to claude as the initial prompt>
```

### 3.2 Schema

The front matter is validated by Pydantic v2 models defined in `symphony/config/schema.py`. The top-level model is `WorkflowConfig`, which composes the following sub-models:

**`TrackerConfig`** (`tracker:`)
| Field | Type | Default | Description |
|---|---|---|---|
| `kind` | `"github"` | `"github"` | Only value currently supported |
| `repo` | `str` | required | Repository in `owner/repo` format |
| `api_token` | `str` | required | GitHub personal access token (use `$GITHUB_TOKEN`) |
| `active_labels` | `list[str]` | `[]` | Labels an issue must have to be dispatched. Empty means any open issue qualifies |
| `skip_labels` | `list[str]` | `["symphony:skip"]` | Issues with any of these labels are ignored |
| `terminal_labels` | `list[str]` | `["symphony:done"]` | Issues with any of these labels are treated as done |

**`PollingConfig`** (`polling:`)
| Field | Type | Default | Description |
|---|---|---|---|
| `interval_ms` | `int` | `30000` | Milliseconds between poll ticks |

**`WorkspaceConfig`** (`workspace:`)
| Field | Type | Default | Description |
|---|---|---|---|
| `root` | `str` | `"./workspaces"` | Base directory for all workspace subdirectories. Resolved relative to WORKFLOW.md at load time |

**`HooksConfig`** (`hooks:`)
| Field | Type | Default | Description |
|---|---|---|---|
| `after_create` | `str` | `""` | Shell command run once when a workspace directory is first created |
| `before_run` | `str` | `""` | Shell command run before each agent attempt |
| `after_run` | `str` | `""` | Shell command run after each agent attempt (failure is logged and ignored) |
| `before_remove` | `str` | `""` | Shell command run before a workspace directory is deleted (failure is logged and ignored) |
| `timeout_ms` | `int` | `60000` | Maximum time any hook may run before being killed |

**`AgentConfig`** (`agent:`)
| Field | Type | Default | Description |
|---|---|---|---|
| `max_concurrent_agents` | `int` | `10` | Global cap on simultaneously running agent tasks |
| `max_turns` | `int` | `20` | Maximum inner turns per issue attempt before the worker exits |
| `max_retry_backoff_ms` | `int` | `300000` | Cap on exponential backoff delay (5 minutes) |
| `max_concurrent_agents_by_state` | `dict[str,int]` | `{}` | Per-state concurrency limits (e.g., `{"active": 3}`) |

**`CodexConfig`** (`codex:`)

The section is named `codex` for spec compatibility; it controls the `claude` CLI.

| Field | Type | Default | Description |
|---|---|---|---|
| `command` | `str` | `"claude"` | The executable to run. Must be on `PATH` |
| `approval_policy` | `"auto"` | `"auto"` | Only `"auto"` is accepted. Pydantic raises `ValidationError` for any other value |
| `turn_timeout_ms` | `int` | `3600000` | Maximum wall-clock time per turn (1 hour) |
| `read_timeout_ms` | `int` | `5000` | Read timeout configuration (stall detection serves a similar purpose) |
| `stall_timeout_ms` | `int` | `300000` | If no stream event arrives for this many ms, the session is killed and retried |

**`ServerConfig`** (`server:`)
| Field | Type | Required | Description |
|---|---|---|---|
| `port` | `int` | yes (if section present) | Port for the HTTP API. Omit the entire `server:` section to disable the API |

**`WorkerConfig`** (`worker:`)
| Field | Type | Default | Description |
|---|---|---|---|
| `ssh_hosts` | `list[str]` | `[]` | Remote hosts in `[user@]host` format for SSH worker |
| `max_concurrent_agents_per_host` | `int` | `3` | Slot cap per SSH host |

### 3.3 Environment Variable Substitution

The `resolve_vars()` function in `symphony/config/loader.py` performs `$VAR` substitution across the entire YAML data structure before Pydantic validation. The substitution rule is: any string value that matches the pattern `^\$([A-Z_][A-Z0-9_]*)$` exactly — a dollar sign followed by an uppercase identifier — is replaced with the corresponding environment variable value.

Key behaviors:
- Only whole-value substitution is supported. `"prefix_$VAR"` is passed through unchanged.
- If the environment variable is not set, `resolve_vars()` raises `ValueError` immediately. Missing variables are a hard startup error.
- Lists and nested dicts are traversed recursively. Every leaf string is checked.
- The `prompt_template` (the Markdown body) is not passed through `resolve_vars()` — it is preserved as a raw string for Liquid rendering.

### 3.4 Path Resolution

After `$VAR` substitution, `load_workflow()` resolves the `workspace.root` path:
1. If the path is already absolute, it is used as-is.
2. `~` is expanded to the user home directory.
3. Relative paths are resolved relative to the directory containing WORKFLOW.md using `(path.parent / root).resolve()`.

The resolved absolute path is written back into the config dict before Pydantic validation, ensuring `WorkflowConfig.workspace.root` is always an absolute path.

---

## 4. GitHub Integration

### 4.1 Issue State Model

Every issue fetched from GitHub is normalized into the `Issue` dataclass (`symphony/tracker/models.py`) and assigned one of three states by `GitHubClient._resolve_state()`:

- **`"active"`** — The issue is open, has no skip or terminal labels, and satisfies `active_labels` (all required labels present, or `active_labels` is empty). Active issues are eligible for dispatch.
- **`"terminal"`** — The issue is closed, or it has at least one label in `terminal_labels`. Terminal issues trigger workspace cleanup and are not dispatched.
- **`"ignored"`** — The issue is open but has a skip label, or has an `active_labels` constraint that is not satisfied. Ignored issues are neither dispatched nor cleaned up.

State resolution applies in strict priority order: closed → terminal label → skip label → active_labels check → active.

### 4.2 The Normalized `Issue` Dataclass

```python
@dataclass
class Issue:
    id: str           # GitHub node_id (string form)
    identifier: str   # "owner/repo#42"
    number: int       # 42
    title: str
    description: str  # issue body text
    state: str        # "active" | "terminal" | "ignored"
    labels: list[str]
    branch_name: str  # "symphony/42-slug-of-title"
    url: str          # HTML URL of the issue
    priority: Optional[int]   # from "priority:N" label, else None
    created_at: datetime
    updated_at: datetime
```

The `identifier` field is constructed as `f"{config.repo}#{number}"` — for example `"myorg/myrepo#42"`. This string is used as the workspace directory name (after sanitization) and as the key in API responses.

The `branch_name` is derived as `f"symphony/{number}-{_slugify(title)}"`. The `_slugify()` function lowercases the title, replaces all non-alphanumeric characters with hyphens, strips leading/trailing hyphens, and truncates to 50 characters.

Priority is parsed from labels matching the pattern `priority:\d+`. Only the first such label is used. If no priority label is present, `priority` is `None`.

### 4.3 GitHub API Operations

`GitHubClient` (`symphony/tracker/github.py`) implements three API operations:

**`fetch_candidate_issues()`** — Called every poll tick. Issues GitHub REST `GET /repos/{owner}/{repo}/issues?state=open&per_page=100` with automatic pagination. Pull requests (items containing a `pull_request` key) are filtered out client-side. Only issues where `_normalize()` produces `state == "active"` are returned.

**`fetch_issues_by_numbers(numbers)`** — Called during reconciliation to check the current state of running sessions. Fires `GET /repos/{owner}/{repo}/issues/{number}` requests in parallel using `asyncio.gather()`. Returns a list of `Issue` objects, silently skipping any 404s.

**`fetch_terminal_issues()`** — Called once at startup. Issues GitHub REST `GET /repos/{owner}/{repo}/issues?state=closed&per_page=100` with pagination. All closed non-PR issues are returned (they are all `"terminal"` by definition). This result drives startup workspace cleanup.

All requests include the headers:
```
Authorization: Bearer <api_token>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
```

### 4.4 Identifier and Branch Name Derivation

Given a GitHub issue with `number=42` and `title="Add dark mode"` in the repository `myorg/myrepo`:

- `identifier` = `"myorg/myrepo#42"`
- `branch_name` = `"symphony/42-add-dark-mode"`

The workspace directory name is derived by sanitizing the identifier with the regex `[^A-Za-z0-9._-]` → `_`, producing `"myorg_myrepo_42"`. This sanitized name is appended to `workspace.root` to form the full workspace path.

---

## 5. Orchestrator

The `Orchestrator` class (`symphony/orchestrator/core.py`) is the heart of Symphony. It owns the poll loop, all shared state, and the logic for dispatching, reconciling, and retrying agent sessions.

### 5.1 In-Memory State

`OrchestratorState` (`symphony/orchestrator/state.py`) is a plain dataclass holding all mutable runtime state:

```python
@dataclass
class OrchestratorState:
    running:      dict[str, LiveSession]  # issue_id → active session
    claimed:      set[str]               # issue IDs reserved to prevent double-dispatch
    retry_queue:  list[RetryEntry]       # sorted ascending by due_at
    completed:    set[str]               # issue IDs that have finished
    token_totals: TokenTotals            # aggregate input/output across all runs
```

`LiveSession` tracks a running agent:
```python
@dataclass
class LiveSession:
    issue:         Issue
    task:          asyncio.Task
    started_at:    datetime              # timezone-aware UTC
    last_event_at: datetime              # updated on every stream-json event
    turn_count:    int
    tokens:        TokenTotals
    session_id:    str
```

`RetryEntry` represents a pending retry:
```python
@dataclass
class RetryEntry:
    issue:    Issue
    attempt:  int       # retry number (1 = first retry)
    due_at:   datetime  # wall-clock time when this retry becomes eligible
    error:    str       # human-readable reason for the retry
```

All mutations to `OrchestratorState` go through a single `asyncio.Lock` (`self._lock`). The lock is held only for in-memory mutations; it is never held across I/O calls.

### 5.2 Startup Cleanup

Before the poll loop begins, `Orchestrator.run()` calls `_startup_cleanup()`. This method calls `fetch_terminal_issues()` and removes each terminal issue's workspace directory (with `hooks_enabled=False`). If the GitHub call fails, the error is logged as a warning and startup continues normally.

### 5.3 The Poll Loop

`Orchestrator.run()` is an infinite loop:
1. Clear `_refresh_event`.
2. Call `_tick()`.
3. Wait for either `_refresh_event` to be set (triggered by `POST /api/v1/refresh`) or for `interval_ms` milliseconds to elapse via `asyncio.wait_for()`.

A manual refresh via the HTTP API triggers an immediate tick without waiting for the full polling interval.

### 5.4 `_tick()` Execution Flow

Each tick executes three phases in sequence:

**Phase 1: `_reconcile()`**
- Collects issue numbers for all currently-running sessions.
- Calls `fetch_issues_by_numbers()` to get fresh state from GitHub.
- For each running session, checks two conditions:
  - **Stall detection**: computes `now = datetime.now(tz=timezone.utc).timestamp()` and `elapsed = now - session.last_event_at.timestamp()`. If `elapsed > stall_timeout_ms / 1000`, cancels the task and schedules a retry with `attempt=1` and error `"stall timeout"`. Both `now` and `last_event_at` use epoch timestamps for a valid comparison.
  - **State change**: if the refreshed issue is now `"terminal"`, cancels the task and fires `_workspace.remove()` as a background task.
- If the GitHub refresh call fails, a warning is logged and all workers continue running.

**Phase 2: `_fire_retries()`**
- Partitions `retry_queue` into due entries (`due_at <= now`) and future entries.
- For each due entry:
  1. Re-fetches the issue from GitHub to check its current state.
  2. If the issue is no longer `"active"`, discards the retry.
  3. If the orchestrator is at capacity, reschedules with error `"no slots"`.
  4. Otherwise, creates a new `asyncio.Task` for `_run_worker()` and adds it to `running`.

**Phase 3: Dispatch**
- Calls `fetch_candidate_issues()`. If this fails, a warning is logged and the tick ends early.
- Sorts issues with `sort_issues()`.
- Iterates through sorted issues, calling `is_eligible()` for each. For each eligible issue, adds to `claimed`, creates a task for `_run_worker()`, and records a `LiveSession`.

### 5.5 Dispatch Eligibility

`is_eligible()` (`symphony/orchestrator/dispatch.py`) checks four conditions:
1. `issue.state == "active"` — non-active issues are never dispatched.
2. `issue.id not in state.claimed and issue.id not in state.running` — prevents double-dispatch.
3. `len(state.running) < config.agent.max_concurrent_agents` — global concurrency limit.
4. Per-state limit: if `max_concurrent_agents_by_state` contains a key matching `issue.state`, the count of running sessions with that state must be below the per-state limit.

### 5.6 Priority Sorting

`sort_issues()` sorts by a three-level key:
1. `priority` ascending (`None` maps to `999` to sort last).
2. `created_at` ascending (oldest issues first within same priority).
3. `number` ascending (tie-breaker).

### 5.7 Retry Backoff Strategy

`retry_delay_ms()` implements two retry modes:

- **Continuation retry** (`attempt=None`): fixed 1,000 ms delay. Used when a worker exits normally after completing all turns — the issue re-enters the queue to be picked up again.
- **Failure retry** (`attempt=N` where N ≥ 1): exponential backoff — `min(10_000 × 2^(attempt-1), max_retry_backoff_ms)`. First failure waits 10s, second 20s, third 40s, up to the configured maximum (default 5 minutes).

`_schedule_retry()` computes `due_at`, appends a `RetryEntry` to `retry_queue`, and re-sorts the queue by `due_at`.

---

## 6. Worker System

### 6.1 Worker Abstract Base

`Worker` (`symphony/worker/base.py`) defines the interface:

```python
class Worker(ABC):
    @abstractmethod
    async def run(
        self,
        issue: Issue,
        config: WorkflowConfig,
        attempt: Optional[int],
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None: ...
```

Implementations must raise on failure (triggering exponential backoff) and return normally on success (triggering a continuation retry).

### 6.2 LocalWorker

`LocalWorker` (`symphony/worker/local.py`) runs `claude` as a local subprocess. The orchestrator always instantiates `LocalWorker` — SSH routing is not yet wired into the orchestrator core, though `SSHWorker` exists and is ready.

The multi-turn loop:
1. `WorkspaceManager.prepare(issue)` — creates or reuses the workspace directory, running `after_create` hook if the directory is new.
2. `WorkspaceManager.run_before_hook(issue)` — runs `before_run` hook in the workspace directory.
3. Inner loop for `turn_idx` in `range(config.agent.max_turns)`:
   - Turn 0: prompt = `render_prompt(config.prompt_template, issue, attempt)`.
   - Turn > 0: prompt = the fixed `_CONTINUATION_PROMPT` string: `"Continue working on the task. Review any progress already made in this workspace and pick up where you left off. Open a pull request when done."`.
   - Calls `ClaudeRunner.run_turn()`. If `result.success` is True, breaks. If False, raises `RuntimeError`.
4. `WorkspaceManager.run_after_hook(issue)` in a `finally` block — always runs even on failure.

### 6.3 SSHWorker

`SSHWorker` (`symphony/worker/ssh.py`) implements the same interface but runs `claude` on a remote host via SSH. The command construction:
1. `ClaudeRunner._build_cmd()` produces the local command list.
2. `_build_remote_cmd()` wraps it: `["ssh", "-T", self._host, f"bash -lc {inner!r}"]` where `inner` is the local command parts joined with single-quote escaping.

The stream reading loop is inlined in `SSHWorker.run()` rather than delegated to `ClaudeRunner.run_turn()`, because the subprocess is the `ssh` command rather than `claude` directly. Event parsing uses the same `parse_stream_event()` function.

`SSHWorker` does not implement per-host slot tracking — described in the design spec but not present in the current orchestrator core.

### 6.4 Hook Lifecycle

| Hook | Trigger | Failure behavior |
|---|---|---|
| `after_create` | First time workspace directory is created | Propagates — aborts attempt, triggers backoff retry |
| `before_run` | Before every agent attempt | Propagates — aborts attempt, triggers backoff retry |
| `after_run` | After every agent attempt | Logged and ignored |
| `before_remove` | Before workspace directory is deleted | Logged and ignored |

All hooks execute with `cwd` set to the workspace directory and are killed after `timeout_ms` milliseconds.

---

## 7. Claude Runner

`ClaudeRunner` (`symphony/agent/claude.py`) launches a single turn of the `claude` CLI and parses its output.

### 7.1 Command Construction

`_build_cmd(prompt, is_continuation)` builds the argument list:

```python
["claude",
 "--print",
 "--output-format", "stream-json",
 "--dangerously-skip-permissions",
 "--max-turns", "1",
 # "--continue" inserted here if is_continuation is True
 "-p", "<prompt text>"]
```

Flags explained:
- `--print`: non-interactive mode; `claude` runs and exits.
- `--output-format stream-json`: emits newline-delimited JSON events to stdout. Required for token counting and stall detection.
- `--dangerously-skip-permissions`: auto-approves all tool calls without user confirmation. Mandatory for unattended operation.
- `--max-turns 1`: Claude does not loop internally. `LocalWorker`'s outer loop controls multi-turn behavior.
- `--continue`: reuses the most recent Claude session in the current working directory. This is how multi-turn continuity is achieved without a persistent session ID.
- `-p "<prompt>"`: the prompt text supplied inline.

The executable name comes from `CodexConfig.command` (default `"claude"`).

### 7.2 Stream-JSON Parsing

`run_turn()` launches the subprocess with `asyncio.create_subprocess_exec()`, then reads stdout line by line. Each line is passed to `parse_stream_event()`.

`parse_stream_event()` parses a JSON object and acts on `type == "result"`:
- `subtype == "success"`: returns `TurnResult(success=True, usage=TokenUsage(...))`.
- Any other subtype: returns `TurnResult(success=False, usage=None, message=...)`.
- All other event types (`assistant`, `tool_use`, `system`, etc.): returns `None`. These still trigger the `on_event` callback for timestamp updates.

After the subprocess exits:
- Non-zero exit and no `result` event parsed → `TurnResult(success=False, message="Exit code N")`.
- Zero exit but no `result` event → `TurnResult(success=False, message="No result event received")`.

### 7.3 Token Accounting

Token data arrives in the `result` event's `usage` field:

```json
{"type": "result", "subtype": "success", "result": "...",
 "usage": {"input_tokens": 12400, "output_tokens": 3100}}
```

The orchestrator's `on_event` callback (defined in `_run_worker()`) intercepts this event and overwrites `session.tokens.input_tokens` and `session.tokens.output_tokens` — overwriting rather than accumulating, because these are cumulative totals that Claude reports per-session.

### 7.4 Stall Detection

Stall detection is the primary timeout mechanism. The orchestrator's `_reconcile()` compares `datetime.now(tz=timezone.utc).timestamp() - session.last_event_at.timestamp()` against `codex.stall_timeout_ms / 1000` on every tick. If the session has been silent longer than the threshold, `session.task.cancel()` is called. Cancellation propagates as `asyncio.CancelledError` through the coroutine chain.

---

## 8. Workspace Manager

`WorkspaceManager` (`symphony/workspace/manager.py`) manages the filesystem directories where agent sessions run.

### 8.1 Directory Path Derivation

Given an issue with `identifier="myorg/myrepo#42"`:
1. `sanitize_identifier("myorg/myrepo#42")` applies the regex `[^A-Za-z0-9._-]` → `_`, producing `"myorg_myrepo_42"`.
2. The workspace path is `(self._root / "myorg_myrepo_42").resolve()`.

### 8.2 Path Traversal Guard

After resolving the path, `_path()` checks that the resolved path starts with `str(self._root.resolve())`. If not, `ValueError` is raised immediately. This guard runs before any filesystem or hook operation.

### 8.3 `prepare(issue, hooks_enabled=True)`

1. Derives the workspace path.
2. Calls `path.mkdir(parents=True, exist_ok=True)` — idempotent.
3. If the directory was just created and `hooks_enabled=True` and `hooks.after_create` is non-empty, runs the hook.
4. Returns the `Path` object.

### 8.4 Hook Execution

`_run_hook(script, cwd)` runs the hook script via `asyncio.create_subprocess_shell()` with `cwd` set to the workspace directory. It waits for completion with `asyncio.wait_for(..., timeout=hooks.timeout_ms / 1000)`. On timeout, the process is killed and `RuntimeError` is raised. On non-zero exit code, `RuntimeError` is raised.

### 8.5 `remove(issue, hooks_enabled=True)`

1. If the directory does not exist, returns immediately.
2. If `hooks_enabled=True` and `before_remove` is non-empty, runs the hook (errors logged, not propagated).
3. Calls `shutil.rmtree(path, ignore_errors=True)`.

---

## 9. Prompt Renderer

`render_prompt()` (`symphony/prompt/renderer.py`) renders the WORKFLOW.md body as a Liquid template.

### 9.1 Liquid Environment

```python
from liquid import Environment, StrictUndefined
_env = Environment(undefined=StrictUndefined)
```

`StrictUndefined` causes any reference to an undefined variable to raise an exception immediately. Misconfigured templates fail loudly rather than silently producing empty strings.

### 9.2 Template Variables

Two top-level variables are available in every render:

**`issue`** — a dict containing all fields of the `Issue` dataclass:
| Variable | Value |
|---|---|
| `issue.id` | GitHub node ID string |
| `issue.identifier` | `"owner/repo#42"` |
| `issue.number` | integer issue number |
| `issue.title` | issue title string |
| `issue.description` | issue body text |
| `issue.state` | `"active"` (always active when dispatched) |
| `issue.labels` | list of label name strings |
| `issue.branch_name` | `"symphony/42-slug-of-title"` |
| `issue.url` | HTML URL of the issue |
| `issue.priority` | integer or `None` |

**`attempt`** — `None` on the first dispatch, or an integer (≥ 1) on retries. Use `{% if attempt %}` to conditionally render retry-specific content.

### 9.3 Example Template

```liquid
You are working on GitHub issue {{ issue.identifier }}: **{{ issue.title }}**.

## Task
{{ issue.description }}

## Context
- Branch: `{{ issue.branch_name }}`
- Labels: {{ issue.labels | join: ", " }}
- Issue URL: {{ issue.url }}
{% if attempt %}
## Retry context
This is attempt **{{ attempt }}**. Review any work already present and continue.
{% endif %}
```

---

## 10. HTTP API

### 10.1 Setup

The FastAPI application is created by `create_app(orchestrator)` (`symphony/api/server.py`). It mounts the router from `build_router(orchestrator)` under the prefix `/api/v1`.

In `symphony/main.py`, if a port is configured, a `uvicorn.Server` is constructed with `host="127.0.0.1"` (localhost-only) and launched as an `asyncio.Task` alongside the orchestrator and dashboard tasks.

### 10.2 Endpoints

**`GET /api/v1/state`**

Returns a full system snapshot:

```json
{
  "running": [
    {
      "issue_identifier": "owner/repo#42",
      "title": "Add dark mode",
      "turn_count": 3,
      "tokens": {"input": 12400, "output": 3100},
      "started_at": "2026-04-28T13:00:00+00:00",
      "last_event_at": "2026-04-28T13:04:12+00:00"
    }
  ],
  "retrying": [
    {
      "issue_identifier": "owner/repo#17",
      "attempt": 2,
      "due_at": "2026-04-28T13:06:00+00:00",
      "error": "claude exited with code 1"
    }
  ],
  "token_totals": {"input": 84200, "output": 21300, "total": 105500},
  "agent_count": {"running": 1, "retrying": 1, "completed": 7}
}
```

**`GET /api/v1/{issue_identifier}`**

Returns the session object for one specific issue. The URL parameter uses `-` in place of `/` and `#` — for `owner/repo#42`, the URL is `/api/v1/owner-repo-42`. The route handler normalizes identifiers with `.replace("/", "-").replace("#", "-")` before comparison.

Returns HTTP 404 if no running session matches.

**`POST /api/v1/refresh`**

Calls `orchestrator.request_refresh()`, which sets the internal `asyncio.Event` that wakes the poll loop early. Returns `{"status": "queued"}`.

### 10.3 Running as Background Task

The uvicorn server runs as `asyncio.create_task(server.serve())` within the same event loop as the orchestrator. Route handlers can read orchestrator state without additional locking for simple reads.

---

## 11. TUI Dashboard

`Dashboard` (`symphony/dashboard/ui.py`) renders a live terminal display using Rich.

### 11.1 Activation

The dashboard is only started when `sys.stdout.isatty()` is True. When stdout is a pipe or redirected, structured log output to stderr is the only feedback mechanism.

### 11.2 Display

`_build_table()` constructs a `rich.table.Table` with three sections:

**Header row**: `"Symphony  ●  N running  N retrying  N completed"` with the current timestamp.

**RUNNING section** (if any): one row per active session showing:
- Issue number (`#42`)
- Title (truncated to 40 characters)
- Turn count
- Input token count (formatted as `12.4k` for values ≥ 1000)
- Output token count formatted similarly
- Session elapsed time in `Xm YYs` or `Xh YYm YYs` format

**RETRYING section** (if any): one row per retry entry showing:
- Issue number and title
- Attempt number
- Countdown to retry (`retry in 92s`)
- Error reason (truncated to 30 characters)

### 11.3 Refresh Rate

`Dashboard.run()` uses a `rich.live.Live` context manager with `refresh_per_second=0.5`. The loop calls `live.update(_build_table(self._orch))` then `await asyncio.sleep(2)` — effective refresh every 2 seconds.

---

## 12. Hot-Reload

`watch_workflow()` (`symphony/config/watcher.py`) uses `watchfiles.awatch()` to watch the WORKFLOW.md file path for changes.

On change:
1. `load_workflow(path)` is called to parse, validate, and resolve the new config.
2. If successful, `on_reload(new_config)` is called. In `symphony/main.py`, this callback does `orch._config = new_config` — a direct attribute assignment.
3. The next poll tick picks up the new config (polling interval, concurrency limits, agent settings, etc.).
4. If loading fails, the error is logged at ERROR level and the orchestrator continues using the last good config.

The watcher runs as a persistent `asyncio.Task` started alongside the orchestrator and HTTP server in `asyncio.gather()`.

---

## 13. Entry Point and CLI

### 13.1 CLI Interface

The `symphony` command is registered via `pyproject.toml`:

```toml
[project.scripts]
symphony = "symphony.main:main"
```

`main()` uses `argparse` with three arguments:

| Argument | Type | Default | Description |
|---|---|---|---|
| `workflow` | positional, optional | `"WORKFLOW.md"` | Path to the WORKFLOW.md file |
| `--port` | int | None | Override the HTTP API port |
| `--log-level` | choice | `"INFO"` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### 13.2 Startup Sequence

`main()` calls `_setup_logging()` then `asyncio.run(_run(workflow_path, port))`.

Inside `_run()`:
1. `load_workflow(workflow_path)` — parse and validate WORKFLOW.md. Hard failure on invalid config or missing env vars.
2. `GitHubClient(config.tracker)` — create the tracker client.
3. `Orchestrator(config, tracker)` — create the orchestrator.
4. If a port is configured: create `uvicorn.Server` and add `server.serve()` task.
5. If `sys.stdout.isatty()`: create `Dashboard` and add `dashboard.run()` task.
6. Add `watch_workflow(workflow_path, _on_reload)` task.
7. Add `orch.run()` task.
8. `await asyncio.gather(*tasks)` — starts everything concurrently.

### 13.3 Logging

`_setup_logging()` configures Python's standard `logging` module:
- Format: `"%(asctime)s %(levelname)s %(name)s %(message)s"` to stderr.
- Level: controlled by `--log-level`.

Structured context (issue_id, issue_identifier) is embedded in log messages as key=value pairs. For example: `"issue_id=node42 issue_identifier=owner/repo#42 turn=1/20 starting"`.

---

## 14. Testing

Tests use `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"` in `pyproject.toml`). The mock HTTP library is `respx`.

### `tests/test_config.py`

Covers `symphony/config/schema.py` and `symphony/config/loader.py`:
- `TrackerConfig` raises `ValidationError` when `repo` or `api_token` are missing
- `TrackerConfig` defaults (`active_labels=[]`, `skip_labels=["symphony:skip"]`, etc.)
- `WorkflowConfig` defaults for all sub-sections
- `CodexConfig` raises `ValidationError` for `approval_policy="manual"`
- `resolve_vars()` substitutes `$MY_TOKEN` from environment
- `resolve_vars()` raises `ValueError` for missing env vars
- `resolve_vars()` handles nested dicts recursively
- `load_workflow()` parses front matter and body, populates `prompt_template`
- `load_workflow()` resolves relative `workspace.root` to an absolute path

### `tests/test_tracker.py`

Covers `symphony/tracker/models.py` and `symphony/tracker/github.py`:
- `Issue` construction and `priority` defaulting
- `_slugify()` basic conversion and 50-character truncation
- `_parse_priority()` finds `priority:N` label and handles absence
- `fetch_candidate_issues()` returns normalized active issue (mocked with `respx`)
- `fetch_candidate_issues()` skips pull requests
- `_resolve_state()` for terminal label, skip label, and `active_labels` requirement
- `_normalize()` produces correct `identifier` and `branch_name`

### `tests/test_prompt.py`

Covers `symphony/prompt/renderer.py`:
- Basic variable rendering and filter usage (`join`)
- `{% if attempt %}` block conditionally rendered
- Unknown variable raises exception (strict undefined enforcement)

### `tests/test_workspace.py`

Covers `symphony/workspace/manager.py`:
- `sanitize_identifier()` replaces `/` and `#` with `_`, preserves safe characters
- `prepare()` creates and reuses directories
- Path traversal guard
- `remove()` deletes the directory

### `tests/test_dispatch.py`

Covers `symphony/orchestrator/dispatch.py`:
- Eligibility: unclaimed under limit → eligible; in `claimed` → ineligible; at limit → ineligible; terminal → ineligible
- `sort_issues()` priority-then-age-then-number ordering
- `retry_delay_ms()`: `attempt=None` → 1000ms; `attempt=1` → 10000ms; large attempt → capped at `max_ms`

### `tests/test_agent.py`

Covers `symphony/agent/claude.py`:
- `parse_stream_event()` returns `None` for non-result events
- Returns successful `TurnResult` with token usage for `subtype="success"`
- Returns failed `TurnResult` with `usage=None` for `subtype="error"`
- `TokenUsage.total` sums input and output

### `tests/test_orchestrator.py`

Covers `symphony/orchestrator/core.py`:
- `_tick()` dispatches an eligible issue and adds it to `running`/`claimed`
- `_tick()` respects `max_concurrent_agents` — 3 issues with limit of 2 → at most 2 dispatched

Tests use `AsyncMock` for the tracker and `patch.object` to replace `_run_worker` with a no-op coroutine.

### `tests/test_api.py`

Covers `symphony/api/server.py` and `symphony/api/routes.py` using FastAPI's `TestClient`:
- `GET /api/v1/state` returns empty lists when state is empty
- `GET /api/v1/state` includes session data when a `LiveSession` is present
- `POST /api/v1/refresh` calls `orchestrator.request_refresh()`
- `GET /api/v1/{identifier}` returns session data for a matching running session
- `GET /api/v1/{identifier}` returns HTTP 404 when no session matches

---

## 15. Key Files Reference

| File | Role |
|---|---|
| `symphony/main.py` | CLI entry point; wires all components; `asyncio.gather` startup |
| `symphony/config/schema.py` | Pydantic v2 models for all WORKFLOW.md config sections |
| `symphony/config/loader.py` | `load_workflow()` and `resolve_vars()` |
| `symphony/config/watcher.py` | `watch_workflow()` — hot-reload via watchfiles |
| `symphony/tracker/models.py` | `Issue` dataclass — the core domain object |
| `symphony/tracker/base.py` | `TrackerClient` ABC |
| `symphony/tracker/github.py` | `GitHubClient` — GitHub REST API, state resolution, normalization |
| `symphony/orchestrator/state.py` | `OrchestratorState`, `LiveSession`, `RetryEntry`, `TokenTotals` |
| `symphony/orchestrator/dispatch.py` | `is_eligible()`, `sort_issues()`, `retry_delay_ms()` |
| `symphony/orchestrator/core.py` | `Orchestrator` — poll loop, dispatch, reconcile, retry, stall detection |
| `symphony/workspace/manager.py` | `WorkspaceManager` — dirs, sanitization, path guard, hooks |
| `symphony/agent/claude.py` | `ClaudeRunner` — subprocess, stream-json parsing, token accounting |
| `symphony/worker/base.py` | `Worker` ABC |
| `symphony/worker/local.py` | `LocalWorker` — multi-turn loop, continuation prompts |
| `symphony/worker/ssh.py` | `SSHWorker` — remote `claude` over SSH stdio |
| `symphony/prompt/renderer.py` | `render_prompt()` — strict Liquid rendering |
| `symphony/api/server.py` | `create_app()` — FastAPI factory |
| `symphony/api/routes.py` | `build_router()` — `/api/v1/*` handlers |
| `symphony/dashboard/ui.py` | `Dashboard` — Rich Live TUI |
| `WORKFLOW.md.example` | Annotated example configuration |
| `pyproject.toml` | Build config, dependencies, `symphony` CLI entry point |
