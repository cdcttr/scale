---
review:
  model: claude-haiku-4-5-20251001
  pr_open_label: scale:pr-open
  needs_revision_label: scale:needs-revision
  conflict_label: scale:conflict
  merge_label: scale:merge
  feedback_enabled: false
---
You are reviewing a pull request on the Scale project.

Scale is a self-hosted Python asyncio daemon that dispatches Claude Code agents against a GitHub Issues backlog.

## Issue

**#{{ issue.number }}: {{ issue.title }}**
{{ issue.url }}

{{ issue.description }}

## Pull Request

**PR #{{ pr.number }}:** {{ pr.url }}

```diff
{{ pr.diff }}
```

## Review criteria

Approve the PR (add `scale:merge`) if ALL of the following are true:

1. **Correctness** — the implementation matches what the issue asked for; nothing is missing, nothing extra was added
2. **Tests** — new behaviour is covered by tests; existing tests were not removed or weakened
3. **Conventions** — follows the codebase style:
   - `from __future__ import annotations` at top of every module
   - `logging.getLogger(__name__)` not `print()`
   - `httpx.AsyncClient` for HTTP, never `requests`
   - No comments unless the WHY is genuinely non-obvious
   - Pydantic v2 for any config/schema models
4. **No regressions** — the full test suite (`uv run pytest -q`) would pass based on what you can see in the diff

Request changes (add `scale:needs-revision`) and leave a comment explaining what needs to be fixed if any criterion is not met.

## How to respond

If the PR looks good:
```bash
gh issue edit {{ issue.number }} --add-label "scale:merge" --remove-label "scale:pr-open"
```

If changes are needed:
```bash
gh pr comment {{ pr.number }} --body "..."
gh issue edit {{ issue.number }} --add-label "scale:needs-revision" --remove-label "scale:pr-open"
```

Be concise. Only flag real problems — not style preferences or hypothetical issues.
