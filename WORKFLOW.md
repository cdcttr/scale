---
tracker:
  kind: github
  repo: cdcttr/scale
  api_token: $GITHUB_TOKEN
  active_labels:
    - symphony:ready
  skip_labels:
    - symphony:skip
    - symphony:concept
    - symphony:planned
  terminal_labels:
    - symphony:done

polling:
  interval_ms: 60000

workspace:
  root: ./workspaces

hooks:
  after_create: |
    git clone https://x-access-token:${GITHUB_TOKEN}@github.com/cdcttr/scale.git . && uv sync
  before_run: |
    git fetch origin main && git checkout main && git reset --hard origin/main
  timeout_ms: 120000

agent:
  max_concurrent_agents: 2
  max_turns: 20

triage:
  model: claude-haiku-4-5-20251001

planner:
  model: claude-sonnet-4-6

# server:
#   port: 8080
#   api_token: $SYMPHONY_API_TOKEN
---
You are implementing a GitHub issue on the Scale project.

Scale is a self-hosted Python asyncio daemon that dispatches Claude Code agents against a GitHub Issues backlog. You are running inside a clone of that repo.

## Issue

**#{{ issue.number }}: {{ issue.title }}**
{{ issue.url }}

{{ issue.description }}
{% if issue.labels %}
**Labels:** {{ issue.labels | join: ", " }}
{% endif %}
{% if attempt %}
**Retry attempt {{ attempt }} — a previous attempt did not complete successfully.**
{% endif %}

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

All AI operations use `ClaudeRunner` (subprocess around the `claude` CLI) — never the Anthropic SDK directly.

## How to work

1. **Read the issue completely** before touching any code. If the issue references specific files or functions, read those first.

2. **Explore before editing.** Read the files relevant to the change. Follow existing patterns — the codebase has consistent style. When in doubt, look at how an analogous thing is already done.

3. **Write failing tests first.** Add tests to `tests/test_<module>.py` before implementing. Confirm they fail: `uv run pytest tests/test_<module>.py::test_name -v`

4. **Implement the minimal change.** Do not refactor unrelated code. Do not add features not asked for.

5. **Run the full suite.** Every existing test must still pass: `uv run pytest -q`

6. **Open a pull request.**

```bash
git checkout -b scale/{{ issue.number }}
git add <files>
git commit -m "<what changed and why in one line>"
gh pr create \
  --title "{{ issue.title }}" \
  --body "Closes #{{ issue.number }}"
```

## Efficiency rules

- Read each file **once**. Do not re-read a file you have already read in this session.
- You have a **20-turn budget**. Write failing tests by turn 3, implementation by turn 6, PR by turn 15.
- Do not invoke brainstorming or planning skills — the issue is your spec. Read it, implement it.
- If you have read more than 5 files and have not written any code, stop exploring and start writing.

## Coding conventions

- No comments unless the WHY is genuinely non-obvious (hidden constraint, subtle invariant, workaround for a specific external bug). Never comment what the code does.
- `from __future__ import annotations` at the top of every module
- Type hints on all function signatures
- Async where the surrounding module is async; sync helpers stay sync
- Pydantic v2 for any new config/schema models
- `httpx.AsyncClient` for all HTTP (not `requests`)
- `logging.getLogger(__name__)` — never `print()` in library code
- `uv` for package management (`uv add`, `uv run`)

## Done

The task is complete when:
- All existing tests pass (`uv run pytest -q`)
- New tests cover the changed behavior
- A PR is open on GitHub targeting `main`, referencing this issue number
