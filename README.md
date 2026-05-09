# Scale

A self-hosted Python daemon that dispatches Claude Code agents against a GitHub Issues backlog. Point it at a repo, write a prompt template, and Scale handles the rest: polling for open issues, spawning Claude Code CLI processes in isolated workspaces, managing concurrency, retrying failures, and cleaning up when work is done.

Inspired by [OpenAI's Symphony](https://github.com/openai/openai-symphony), reimplemented for Claude Code.

---

## How it works

1. You label a GitHub issue `symphony:ready`
2. Scale picks it up, clones the repo into an isolated workspace, and runs `claude` with your prompt template rendered against the issue
3. The agent reads the issue, implements the change, opens a PR
4. Scale adds `symphony:done` and moves to the next issue

Failures retry with exponential backoff. Concurrency is bounded by `max_concurrent_agents`. A live TUI dashboard shows what's running.

---

## Installation

Requires Python 3.12+ and the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) on PATH.

```bash
git clone https://github.com/cdcttr/openai-symphony
cd openai-symphony
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
    - symphony:ready
  terminal_labels:
    - symphony:done

polling:
  interval_ms: 60000

workspace:
  root: ./workspaces

hooks:
  after_create: |
    git clone https://x-access-token:${GITHUB_TOKEN}@github.com/your-org/your-repo.git . && uv sync
  before_run: |
    git fetch origin main && git checkout main && git reset --hard origin/main
  timeout_ms: 120000

agent:
  max_concurrent_agents: 2
  max_turns: 20
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

Triage labels issues `symphony:ready` or `symphony:needs-detail` and posts a comment explaining the verdict. Issues are re-triaged automatically when updated.

**Decompose high-level issues into leaf tasks** (uses Claude Sonnet):

```bash
scale plan WORKFLOW.md --issue 7
scale plan WORKFLOW.md --issue 7 --dry-run
```

Requires a `planner:` section in WORKFLOW.md. Label a parent issue `symphony:plan` and the planner will either classify it as directly implementable (`symphony:leaf`) or break it into child issues (`symphony:planned`).

---

## Commands

```
scale run WORKFLOW.md [--port N] [--log-level DEBUG|INFO|WARNING|ERROR]
scale triage WORKFLOW.md [--issue N[,N,...]] [--all] [--model MODEL] [--dry-run]
scale plan WORKFLOW.md --issue N[,N,...] [--force] [--dry-run]
scale version
```

---

## Label lifecycle

| Label | Meaning |
|---|---|
| `symphony:ready` | Issue is ready for dispatch |
| `symphony:triaged` | Triage has run at least once |
| `symphony:needs-detail` | Triage found the issue underspecified |
| `symphony:plan` | Issue should be decomposed before dispatch |
| `symphony:leaf` | Planner classified as directly implementable |
| `symphony:concept` | Planner classified as needing decomposition |
| `symphony:planned` | Children created; parent waiting for them to finish |
| `symphony:done` | Terminal — agent completed, workspace removed |
| `symphony:skip` | Ignored by Scale |

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

## Debugging

Each workspace gets an `agent.log` file written during the run:

```
workspaces/<workspace-name>/agent.log
```

It contains the full rendered prompt, every streaming event from Claude as JSON, and the result + stderr for each turn. Check it when an agent fails.

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

See `docs/IMPLEMENTATION.md` for a detailed walkthrough.

---

## Development

```bash
uv run pytest -q        # run tests
uv run pytest -v        # verbose
```

All AI operations go through `ClaudeRunner` (a subprocess wrapper around `claude --output-format stream-json`) — never the Anthropic SDK directly.
