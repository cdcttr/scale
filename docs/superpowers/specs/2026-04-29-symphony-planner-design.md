# Symphony Planner Design

**Date:** 2026-04-29  
**Status:** Approved

---

## Motivation

Symphony's dispatch loop assumes issues are already leaf-level tasks — bounded, implementation-ready specs an agent can execute directly. In practice, many real-world GitHub Issues are higher-level concepts: "Add OAuth support", "Migrate to PostgreSQL", "Build admin dashboard". Pointing Symphony at these without decomposition produces low-quality PRs or failures.

The Planner adds a decomposition layer that sits between issue creation and dispatch. A human (or process) marks a high-level issue for planning; the planner breaks it into leaf-level child issues; the dispatch loop executes the children. The parent stays open and closes automatically when all children are done.

---

## Architectural Principle: Claude Code CLI for All AI Operations

All AI operations in Symphony — triage, planning, and execution — use the `claude` CLI via `ClaudeRunner`. No direct Anthropic SDK calls anywhere in the codebase. This provides:

- Consistent tool access (Claude Code's file browser, shell, etc.) for every agent
- A single configuration point for the claude binary (`codex.command`)
- Consistent token tracking via stream-json output
- Removal of the `anthropic` library dependency

**Impact on triage:** `symphony/triage/agent.py` is refactored from `Anthropic().messages.create()` to `ClaudeRunner`. The prompt and JSON output contract are unchanged; only the invocation mechanism changes. `ClaudeRunner` gains an optional `--model` flag so triage (Haiku) and planning (Sonnet) can specify different models.

---

## Triggering Model

Symphony does not autonomously classify all unlabeled issues. It only touches issues it is explicitly told to touch. Two trigger paths:

**1. CLI command:**
```
symphony plan --issue 42           # plan a specific issue
symphony plan --issue 42,43,44     # plan multiple
symphony plan --dry-run            # print decomposition, don't create issues or apply labels
symphony plan --force              # re-decompose even if already symphony:planned
symphony plan --log-level DEBUG
```

Targets specific issues for decomposition. When run against an issue already labeled `symphony:planned`, exits with an info log unless `--force` is passed.

**2. Label trigger:**  
Add `symphony:plan` to an issue (manually, via triage, or via another automation). The dispatch loop detects this label and decomposes the issue on the next tick.

There is no `--all` flag and no autonomous scanning of unlabeled issues. Symphony ignores the rest of the backlog.

---

## Label State Machine

Five labels govern the planner lifecycle. All are configurable in `PlannerConfig`; these are the defaults:

| Label | Meaning |
|---|---|
| `symphony:plan` | Human-signaled intent to decompose this issue |
| `symphony:leaf` | Classified as a leaf task — dispatch executes normally |
| `symphony:concept` | Classified as a concept — decomposition in progress or complete |
| `symphony:planned` | Children created — parent awaiting their completion |
| `symphony:done` | Terminal — existing label, used to close the parent when all children complete |

State transitions:

```
Issue with symphony:plan label
  └─► PlannerRunner.plan_issue()
        ├─► PlannerAgent returns "leaf"
        │     → add symphony:leaf, remove symphony:plan
        │     → dispatch loop executes on next tick
        │
        └─► PlannerAgent returns "concept" + children[]
              → create child issues on GitHub
              → link children to parent (sub-issues API or marker comment)
              → add symphony:concept + symphony:planned to parent
              → remove symphony:plan from parent
              → parent skipped by dispatch loop (in skip_labels)
              → _watch_planned(): all children terminal
                    → add symphony:done to parent, remove symphony:planned
```

**Depth enforcement:** Children are created with a `symphony:depth:N` label (root-level issues have no depth label and are treated as depth 0; children created from them get `symphony:depth:1`, and so on). `PlannerRunner` checks this label before calling the agent — issues at `max_depth` are forced to `"leaf"` without an API call.

**Priority:** Issue ordering uses explicit `priority:N` labels, with creation date as the only tiebreaker among equal-priority issues. Issue number is not used for ordering.

---

## Dispatch Loop Changes

`_tick()` in `orchestrator/core.py` gets one new check before claiming an issue:

1. If the issue has `symphony:plan` → call `PlannerRunner.plan_issue(issue)`, then skip dispatch for this tick (the issue will re-enter next tick with `symphony:leaf` or `symphony:planned`)
2. Otherwise → existing dispatch logic unchanged

`symphony:planned` is added to the `skip_labels` config so the orchestrator never attempts to execute a parent that is waiting on children.

A new async task `_watch_planned()` is launched as a separate `asyncio.create_task()` in `Orchestrator.run()` alongside the main tick loop. It runs on the same polling interval as `_tick()`. It:
1. Fetches all issues labeled `symphony:planned`
2. For each, checks whether all children are in a terminal state (closed or labeled `symphony:done`)
3. If all children are terminal: adds `symphony:done` to the parent, removes `symphony:planned`

---

## GitHub Child Tracking

### Primary: Sub-issues API

After creating each child issue, the runner calls:
```
POST /repos/{owner}/{repo}/issues/{parent_number}/sub_issues
Body: {"sub_issue_id": <child_node_id>}
```

GitHub renders these as a native task list in the issue UI. Completion is checked via:
```
GET /repos/{owner}/{repo}/issues/{parent_number}/sub_issues
```

### Fallback: Marker Comment

If the sub-issues API returns 404 or 403, the runner falls back to posting a hidden marker comment on the parent:

```
<!-- symphony-plan {"children": [51, 52, 53], "depth": 1} -->
```

The marker pattern mirrors the triage marker (`<!-- symphony-triage ... -->`). Completion is checked by fetching each child issue individually.

**Both modes:** A marker comment is always written, even when using the sub-issues API. This provides a stable machine-readable record that does not depend on the API being available in future ticks. The fallback mode is detected on first call per repo and does not re-probe.

---

## PlannerAgent

**Location:** `symphony/planner/agent.py`

**Invocation:** `ClaudeRunner` with `--max-turns 1 --model <planner_model>` in the shared planner workspace (`workspaces/_planner/` by default, repo checked out via `after_create` hook). Claude Code's tool access lets it browse the codebase when deciding how to decompose an issue.

**Prompt:** Contains the issue title, body, labels, comments (up to 20, newest last), and current depth. Instructs Claude to return a JSON object as its only output:

```json
{
  "type": "leaf",
  "children": null
}
```

or:

```json
{
  "type": "concept",
  "children": [
    {
      "title": "...",
      "description": "...",
      "labels": ["symphony:ready"]
    }
  ]
}
```

**Result parsing:** `TurnResult.message` is parsed as JSON. If parsing fails, the issue is left unmodified and retried next tick.

**Recursive decomposition:** Supported but not expected to be the norm. Children with no classification label will be detected with `symphony:plan` behavior on the next tick if a human adds the label, or will be executed directly if labeled `symphony:ready`. Max depth (`max_depth`, default 3) is enforced by `PlannerRunner` before calling the agent.

---

## TriageAgent Refactor

**Location:** `symphony/triage/agent.py`

`TriageAgent` drops the `Anthropic` client. It uses `ClaudeRunner` with `--max-turns 1 --model <triage_model>` in an ephemeral temp directory (no codebase access needed — triage reasons only about issue content). The existing prompt and JSON output contract are unchanged. The `anthropic` library is removed from `pyproject.toml`.

---

## ClaudeRunner Changes

`symphony/agent/claude.py`:

- `_build_cmd` accepts an optional `model: str | None` parameter
- When set, `--model <model>` is added to the subprocess command
- `run_turn` signature gains `model: str | None = None` and passes it through

---

## Config Schema

New `PlannerConfig` in `symphony/config/schema.py`:

```python
class PlannerConfig(BaseModel):
    model: str = "claude-sonnet-4-6"
    max_depth: int = 3
    plan_label: str = "symphony:plan"
    leaf_label: str = "symphony:leaf"
    concept_label: str = "symphony:concept"
    planned_label: str = "symphony:planned"
    planner_workspace: str = "./workspaces/_planner"
```

`WorkflowConfig` gains:
```python
planner: Optional[PlannerConfig] = None
```

Planning is opt-in. If `planner` is absent, the dispatch loop ignores `symphony:plan` labels and `symphony plan` errors with a clear message.

`TriageConfig.model` remains as-is. `ClaudeRunner` now uses it via the new `model` parameter.

---

## GitHub Client Additions

New methods on `GitHubClient`:

- `create_issue(title: str, body: str, labels: list[str]) -> dict` — creates and returns the new issue object (includes `node_id` and `number`)
- `add_sub_issue(parent_number: int, child_node_id: str) -> bool` — returns False if API unavailable (404/403), True on success
- `fetch_sub_issues(parent_number: int) -> list[dict]` — returns child issue objects; empty list if API unavailable
- `fetch_issues_by_label(label: str) -> list[Issue]` — fetches all open issues carrying a specific label; used by `_watch_planned()` to find parents awaiting child completion

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Classification API failure (subprocess error or bad JSON) | Issue left unmodified; retried next tick |
| Partial child creation failure | Successfully created children remain; parent not labeled `symphony:planned`; full retry next tick |
| Duplicate child titles | Runner checks for existing open issues with the same title; adopts existing rather than creating duplicate |
| Sub-issues API unavailable | Detected on first call; marker comment fallback used for all subsequent operations |
| Parent completion race (child mid-run) | `_watch_planned()` does not close parent; child is not terminal yet; checks again next tick |
| Max depth exceeded | `PlannerRunner` forces `"leaf"` classification without calling the agent |

---

## Files Created or Modified

| File | Change |
|---|---|
| `symphony/planner/__init__.py` | New — empty package init |
| `symphony/planner/agent.py` | New — `PlannerAgent` using `ClaudeRunner` |
| `symphony/planner/runner.py` | New — `PlannerRunner`, GitHub API calls, label lifecycle |
| `symphony/triage/agent.py` | Refactored — replace Anthropic SDK with `ClaudeRunner` |
| `symphony/agent/claude.py` | Modified — add optional `--model` flag to `_build_cmd` / `run_turn` |
| `symphony/orchestrator/core.py` | Modified — `symphony:plan` check in `_tick()`, new `_watch_planned()` task |
| `symphony/config/schema.py` | Modified — add `PlannerConfig`, add `planner` field to `WorkflowConfig` |
| `symphony/tracker/github.py` | Modified — add `create_issue`, `add_sub_issue`, `fetch_sub_issues` |
| `symphony/main.py` | Modified — add `symphony plan` CLI subcommand |
| `pyproject.toml` | Modified — remove `anthropic` dependency |
| `tests/test_planner.py` | New — unit tests for `PlannerAgent`, `PlannerRunner` |
| `tests/test_triage.py` | Modified — update for `ClaudeRunner`-based `TriageAgent` |
| `tests/test_agent.py` | Modified — update `ClaudeRunner` tests for `model` param |
