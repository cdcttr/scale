# Symphony Triage Agent — Design Spec

## Overview

Symphony dispatches Claude Code agents against GitHub Issues autonomously, but not all issues are equally well-defined. Vague, under-specified, or discussion-style issues waste agent turns. The triage agent provides a pre-dispatch quality gate: it reads an issue, assesses whether it is specific and actionable enough for autonomous implementation, posts a structured comment explaining its verdict, and applies labels so that Symphony's dispatch loop only picks up issues that have been cleared.

Triage is a manual CLI command, not part of the daemon poll loop. It is run once per issue and re-runs automatically only when new information has been added to the issue (edit or comment) since the last triage.

---

## Goals

- Assess issue readiness using a fast, cheap LLM (Haiku by default)
- Post a human-readable comment on the GitHub issue with the assessment
- Apply `symphony:ready` or `symphony:needs-detail` labels automatically
- Skip issues that have already been triaged and not updated since
- Support targeted triage (one issue, a set, or all) and a `--dry-run` mode
- Integrate cleanly with the existing `symphony run` dispatch loop via `active_labels`

---

## Architecture

### New module: `symphony/triage/`

```
symphony/triage/__init__.py
symphony/triage/agent.py      — TriageAgent: calls Anthropic SDK, produces TriageAssessment
symphony/triage/runner.py     — TriageRunner: orchestrates fetch → assess → post → label
```

### Extended: `symphony/tracker/github.py`

New methods on `GitHubClient`:
- `fetch_issue_comments(number)` — `GET /issues/{number}/comments`
- `post_comment(number, body)` — `POST /issues/{number}/comments`
- `add_labels(number, labels)` — `POST /issues/{number}/labels`
- `remove_label(number, label)` — `DELETE /issues/{number}/labels/{name}` (404-tolerant)
- `fetch_single_issue(number)` — `GET /issues/{number}` (already partially exists via fetch_issues_by_numbers)

### Extended: `symphony/config/schema.py`

New `TriageConfig` model, added to `WorkflowConfig`.

### Extended: `symphony/main.py`

New `triage` subcommand alongside `run` and `version`.

### New dependency

`anthropic>=0.40` added to `pyproject.toml`. The Anthropic SDK is used directly (not the `claude` CLI subprocess) because triage needs no tool use — only reasoning and structured text output.

---

## Configuration Schema

```yaml
triage:
  model: claude-haiku-4-5-20251001      # model used for assessment
  ready_label: symphony:ready            # applied when issue passes
  needs_detail_label: symphony:needs-detail  # applied when issue fails
  triaged_label: symphony:triaged        # always applied after any triage run
```

All fields have defaults, so the `triage:` section is optional. `TriageConfig` is an optional field on `WorkflowConfig` (like `ServerConfig`).

```python
class TriageConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    ready_label: str = "symphony:ready"
    needs_detail_label: str = "symphony:needs-detail"
    triaged_label: str = "symphony:triaged"
```

---

## Re-Triage Detection

Every triage comment contains a hidden HTML comment on its first line with an ISO 8601 timestamp:

```
<!-- symphony-triage 2026-04-28T14:30:00Z -->
## Symphony Triage
...
```

**Decision logic for a given issue:**

1. Fetch all comments on the issue.
2. Find the most recent comment whose body starts with `<!-- symphony-triage `.
3. Parse the timestamp from the marker.
4. Compare with `issue.updated_at`:
   - If `issue.updated_at <= triage_timestamp` → **skip** (already current)
   - If `issue.updated_at > triage_timestamp` → **re-triage** (new information added)
5. If no triage comment exists → **triage** (first time).
6. If `--all` flag is set → **triage** regardless of timestamps.

`issue.updated_at` is set by GitHub whenever the issue body is edited or any comment is added, so it reliably captures "new information."

---

## Triage Agent

### Model selection

Default: `claude-haiku-4-5-20251001`. Overridable via `triage.model` in WORKFLOW.md or `--model` CLI flag. The model needs to: read a GitHub issue, reason about its completeness, and produce structured JSON. Haiku handles this well for clear-cut cases. If nuanced judgment is needed on an issue-by-issue basis, the operator can switch to Sonnet via the flag.

### Input to the LLM

The prompt includes:
- Issue title
- Issue body (full text)
- All existing comments (author + body, newest last, truncated at 20 comments)
- The list of current labels
- The assessment criteria

### Assessment criteria

An issue is **ready** if all of the following are true:
- The task is clearly stated (not a question or open-ended discussion)
- The scope is bounded — there is a defined "done" state
- Sufficient context is present to begin implementation without asking for clarification
- It is a coding/engineering task (not a process, policy, or design discussion)

An issue is **not ready** if any of the following apply:
- The title or body is vague (e.g., "fix the bug", "make it faster")
- Critical information is missing (which component, what behaviour, what the expected state is)
- The issue is a question or a discussion thread
- Multiple unrelated tasks are bundled together
- It depends on an external decision that has not been made

### LLM output format

The model is instructed to respond with a JSON object only, no prose outside the JSON:

```json
{
  "ready": true,
  "summary": "One-sentence verdict",
  "reasons": ["Only populated if not ready — specific gaps"],
  "comment": "Full markdown comment body to post on GitHub"
}
```

The `comment` field is what Symphony posts verbatim to GitHub (after prepending the hidden marker line).

### Failure handling

- If the Anthropic API call fails (network, rate limit, etc.) → log the error, skip the issue, continue with remaining issues
- If JSON parsing fails → log the raw response, skip the issue
- No retries on API failure (triage is manual and can be re-run)

---

## Label Lifecycle

| Triage result | Labels added | Labels removed |
|---|---|---|
| Ready | `symphony:ready`, `symphony:triaged` | `symphony:needs-detail` |
| Not ready | `symphony:needs-detail`, `symphony:triaged` | `symphony:ready` |

Label removal is best-effort: a 404 (label not present) is silently ignored.

After a human adds detail and the issue is re-triaged:
- If it now passes: `symphony:needs-detail` is removed, `symphony:ready` is added
- If it still fails: comment is updated with new specific gaps

---

## Comment Format

```markdown
<!-- symphony-triage 2026-04-28T14:30:00Z -->
## Symphony Triage

**Status: Ready ✅**

This issue is clear and actionable. The scope is well-defined and there is sufficient context to begin implementation.
```

```markdown
<!-- symphony-triage 2026-04-28T14:30:00Z -->
## Symphony Triage

**Status: Needs more detail ❌**

This issue needs clarification before it can be worked on autonomously:

- The expected behaviour after the fix is not described — only the current (broken) behaviour is mentioned
- It is unclear which endpoint or service is affected
- No acceptance criteria are provided

Please add more detail and Symphony will re-evaluate when the issue is updated.
```

---

## CLI Interface

```
symphony triage [WORKFLOW]
```

**Arguments:**

| Flag | Description |
|---|---|
| `WORKFLOW` | Path to WORKFLOW.md (default: `./WORKFLOW.md`) |
| `--issue/-i N[,N,...]` | Triage only these issue numbers |
| `--all` | Force re-triage all issues, even if already current |
| `--model MODEL` | Override the LLM model for this run |
| `--dry-run` | Print assessment to stdout, do not post to GitHub or apply labels |
| `--log-level` | DEBUG / INFO / WARNING / ERROR (default: INFO) |

**Behaviour with no flags:** fetch all open issues, apply re-triage detection, triage only those that need it.

---

## Integration with Dispatch Loop

To use triage as a dispatch gate, set `active_labels` in the tracker config to include `symphony:ready`:

```yaml
tracker:
  repo: owner/repo
  api_token: $GITHUB_TOKEN
  active_labels:
    - symphony:ready
```

This means the daemon will only dispatch issues that the triage agent has cleared. Issues labelled `symphony:needs-detail` are ignored until they are updated and re-triaged.

---

## File Map

| File | Change |
|---|---|
| `pyproject.toml` | Add `anthropic>=0.40` dependency |
| `symphony/config/schema.py` | Add `TriageConfig`, add optional `triage` field to `WorkflowConfig` |
| `symphony/tracker/github.py` | Add `fetch_issue_comments`, `post_comment`, `add_labels`, `remove_label` |
| `symphony/triage/__init__.py` | New (empty) |
| `symphony/triage/agent.py` | `TriageAgent`: Anthropic API call, prompt construction, JSON parsing |
| `symphony/triage/runner.py` | `TriageRunner`: orchestrates per-issue triage flow |
| `symphony/main.py` | Add `triage` subcommand |
| `tests/test_triage_agent.py` | Unit tests for TriageAgent |
| `tests/test_triage_runner.py` | Unit tests for TriageRunner |
| `tests/test_tracker.py` | Tests for new GitHubClient methods |
