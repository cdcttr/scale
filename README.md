# Scale

A self-hosted Python daemon that dispatches Claude Code agents against a GitHub Issues backlog. Point it at a repo, write a prompt template, and Scale handles the rest: polling for open issues, spawning Claude Code CLI processes in isolated workspaces, managing concurrency, retrying failures, and cleaning up when work is done.

Inspired by [OpenAI's Symphony](https://github.com/openai/openai-symphony), reimplemented for Claude Code.

---

## How it works

1. You label a GitHub issue `scale:ready`
2. Scale picks it up, clones the repo into an isolated workspace, and runs `claude` with your prompt template rendered against the issue
3. The agent reads the issue, implements the change, opens a PR
4. Scale adds `scale:done` and moves to the next issue

Failures retry with exponential backoff. Concurrency is bounded by `max_concurrent_agents`. A live TUI dashboard shows what's running.

---

## Installation

Requires Python 3.12+ and the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) on PATH.

```bash
git clone https://github.com/cdcttr/scale
cd scale
uv sync
```

---

## Setup

Copy `WORKFLOW.md.example` to `WORKFLOW.md` and edit the frontmatter:

```yaml
tracker:
  kind: github
  repo: your-org/your-repo
  api_token: $GITHUB_TOKEN
  active_labels:
    - scale:ready
  skip_labels:
    - scale:skip
    - scale:concept
    - scale:planned
  terminal_labels:
    - scale:done

polling:
  interval_ms: 60000

workspace:
  root: ./workspaces
  # log_archive: ./logs   # optional: copy agent.log here before workspace cleanup

hooks:
  after_create: |
    git clone https://x-access-token:${GITHUB_TOKEN}@github.com/your-org/your-repo.git . && uv sync
  before_run: |
    git fetch origin main && git checkout main && git reset --hard origin/main
  timeout_ms: 120000

agent:
  max_concurrent_agents: 2
  max_turns: 20

# triage:                          # optional: enable automated triage assessment
#   model: claude-haiku-4-5-20251001

# planner:                         # optional: enable issue decomposition
#   model: claude-sonnet-4-6

# review:                          # optional: post-run PR review phase
#   pr_open_label: scale:pr-open
#   needs_revision_label: scale:needs-revision
#   conflict_label: scale:conflict
```

The body of `WORKFLOW.md` (below the frontmatter) is a [Liquid](https://shopify.github.io/liquid/) template rendered as the agent's prompt. Variables available: `{{ issue.number }}`, `{{ issue.title }}`, `{{ issue.description }}`, `{{ issue.url }}`, `{{ issue.labels }}`, `{{ attempt }}`.

Credentials go in the environment, never in the file:

```bash
export GITHUB_TOKEN=$(gh auth token)
```

---

## Usage

**Run the dispatch loop:**

```bash
scale run WORKFLOW.md
```

**Pre-screen issues before dispatch** (uses Claude Haiku to assess readiness):

```bash
scale triage WORKFLOW.md --issue 12
scale triage WORKFLOW.md          # all open issues
scale triage WORKFLOW.md --dry-run
```

Triage reads each issue and labels it `scale:ready`, `scale:needs-detail`, or `scale:needs-approval`, and posts a comment explaining the verdict. Requires a `triage:` section in `WORKFLOW.md`. Issues labeled `scale:triage` are automatically triaged on each poll cycle.

**Decompose high-level issues into leaf tasks** (uses Claude Sonnet):

```bash
scale plan WORKFLOW.md --issue 7
scale plan WORKFLOW.md --issue 7 --dry-run
```

Requires a `planner:` section in WORKFLOW.md. Label a parent issue `scale:plan` and the planner will either classify it as directly implementable (`scale:leaf`) or break it into child issues (`scale:planned`).

**Remove stale workspace directories:**

```bash
scale clean WORKFLOW.md
scale clean WORKFLOW.md --dry-run
scale clean WORKFLOW.md --all --yes
```

---

## Commands

```
scale run    [WORKFLOW.md] [--port N] [--log-level DEBUG|INFO|WARNING|ERROR]
scale triage [WORKFLOW.md] [--issue N[,N,...]] [--all] [--model MODEL] [--dry-run]
scale plan   [WORKFLOW.md] --issue N[,N,...] [--force] [--dry-run]
scale clean  [WORKFLOW.md] [--dry-run] [--all] [--yes]
scale version
```

All subcommands default to `WORKFLOW.md` in the current directory if no path is given.

---

## Label lifecycle

| Label | Meaning |
|---|---|
| `scale:triage` | Opt in to automated triage assessment on next poll |
| `scale:triaged` | Triage has run at least once |
| `scale:ready` | Issue is ready for dispatch |
| `scale:needs-detail` | Triage found the issue underspecified |
| `scale:needs-approval` | Well-specified but needs human sign-off before dispatch |
| `scale:supervised` | Blocks dispatch even if `scale:ready`; requires human to remove |
| `scale:plan` | Issue should be decomposed before dispatch |
| `scale:leaf` | Planner: directly implementable without decomposition |
| `scale:concept` | Planner: decomposed into child issues |
| `scale:planned` | Children created; parent waiting for them to finish |
| `scale:done` | Terminal — agent completed, workspace removed |
| `scale:skip` | Ignored by Scale entirely |

---

## Workspace hooks

Hooks run shell scripts at lifecycle points in each workspace:

| Hook | When |
|---|---|
| `after_create` | Once, after workspace directory is first created |
| `before_run` | Before each agent turn sequence (use to reset to clean state) |
| `after_run` | After each agent turn sequence |
| `before_remove` | Before workspace directory is deleted |

Hooks inherit the parent process environment, so `$GITHUB_TOKEN` and other variables are available.

---

## Observability

**Agent logs** — each workspace gets an `agent.log` written during the run:

```
workspaces/<workspace-name>/agent.log
```

It contains the full rendered prompt, every streaming event from Claude as JSON, and the result and stderr for each turn. If `workspace.log_archive` is set in `WORKFLOW.md`, Scale copies the log here before removing the workspace so it survives cleanup.

**Per-issue stats** — Scale appends a JSON record to `stats.jsonl` in the project root after each issue completes:

```jsonl
{"issue": 42, "turns": 8, "input_tokens": 9400, "output_tokens": 3000, "duration_s": 94, "attempt": 1, "success": true, "timestamp": "2026-05-09T22:00:00Z", "issue_title": "Fix the thing"}
```

**Log analysis** — `scripts/analyze_agent_logs.py` reads `stats.jsonl` and prints a summary table:

```bash
python scripts/analyze_agent_logs.py
python scripts/analyze_agent_logs.py path/to/stats.jsonl
```

**Tick summaries** — the dispatch loop logs a one-line summary every poll cycle showing active agents, queue depth, and recent completions.

---

## Running Scale on itself

This repo includes a `WORKFLOW.md` configured to dispatch agents against its own GitHub Issues. To use it:

```bash
export GITHUB_TOKEN=$(gh auth token)
scale triage WORKFLOW.md --issue N
scale run WORKFLOW.md
```

---

## Architecture

Single Python asyncio process. The `Orchestrator` owns in-memory state and drives the poll-and-dispatch loop. Every agent runs as a `claude` CLI subprocess in an isolated workspace directory. The prompt template is rendered per issue using Liquid. The HTTP API (optional) and Rich TUI dashboard share the same orchestrator state object.

---

## Development

```bash
uv run pytest -q        # run tests
uv run pytest -v        # verbose
```

All AI operations go through `ClaudeRunner` (a subprocess wrapper around `claude --output-format stream-json`) — never the Anthropic SDK directly.
