# Scale — Contributor Guide

Scale is a self-hosted Python asyncio daemon that dispatches Claude Code agents against a GitHub Issues backlog.

## Development setup

```bash
git clone https://github.com/cdcttr/scale
cd scale
uv sync
uv run pytest -q   # verify everything passes
```

Requires Python 3.12+ and the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude`) on PATH.

## Running Scale on itself

```bash
export GITHUB_TOKEN=$(gh auth token)
scale run WORKFLOW.md
```

## Project layout

```
scale/
  agent/claude.py        — ClaudeRunner: wraps claude CLI subprocess, stream-json parsing
  config/schema.py       — Pydantic v2 config models (WorkflowConfig and friends)
  config/loader.py       — WORKFLOW.md frontmatter parser, $VAR env substitution
  orchestrator/core.py   — main poll/dispatch loop, _watch_planned
  planner/agent.py       — PlannerAgent: decomposes high-level issues via ClaudeRunner
  planner/runner.py      — PlannerRunner: label lifecycle, GitHub child tracking
  tracker/github.py      — GitHub REST API client
  tracker/base.py        — TrackerClient abstract base
  triage/agent.py        — TriageAgent: issue readiness assessment via ClaudeRunner
  triage/runner.py       — TriageRunner
  worker/local.py        — LocalWorker: multi-turn claude loop
  workspace/manager.py   — per-issue workspace setup and hook execution
  prompt/renderer.py     — Liquid template renderer (strict undefined)
  main.py                — CLI entry point
tests/                   — pytest suite; run with: uv run pytest
```

## Key conventions

- All AI operations go through `ClaudeRunner` (subprocess around `claude` CLI) — never the Anthropic SDK directly
- `from __future__ import annotations` at the top of every module
- `httpx.AsyncClient` for HTTP, never `requests`
- `logging.getLogger(__name__)`, never `print()` in library code
- Pydantic v2 for config/schema models
- `uv` for package management (`uv add`, `uv run`)

## Testing

```bash
uv run pytest -q                                      # full suite
uv run pytest -v                                      # verbose
uv run pytest tests/test_<module>.py::test_name -v   # single test
```

## Label lifecycle

| Label | Meaning |
|---|---|
| `scale:ready` | Issue is ready for dispatch |
| `scale:supervised` | Requires human approval before dispatch |
| `scale:triaged` | Triage has run at least once |
| `scale:needs-detail` | Triage found the issue underspecified |
| `scale:needs-approval` | Waiting for human approval |
| `scale:plan` | Issue should be decomposed before dispatch |
| `scale:leaf` | Planner classified as directly implementable |
| `scale:concept` | Planner found issue needs decomposition; skipped by dispatcher |
| `scale:planned` | Children created; parent waiting for them to finish; skipped by dispatcher |
| `scale:done` | Terminal — agent completed, workspace removed |
| `scale:skip` | Permanently ignored by Scale |

The dispatcher picks up issues labelled `scale:ready` (and not `scale:skip`, `scale:concept`, or `scale:planned`). `scale:done` is terminal — Scale will not touch that issue again.
