from __future__ import annotations
from typing import Optional
from liquid import Environment, StrictUndefined
from symphony.tracker.models import Issue

_env = Environment(undefined=StrictUndefined)


def render_prompt(template: str, issue: Issue, attempt: Optional[int]) -> str:
    tmpl = _env.from_string(template)
    issue_ctx = {
        "id": issue.id,
        "identifier": issue.identifier,
        "number": issue.number,
        "title": issue.title,
        "description": issue.description,
        "state": issue.state,
        "labels": issue.labels,
        "branch_name": issue.branch_name,
        "url": issue.url,
        "priority": issue.priority,
    }
    return tmpl.render(issue=issue_ctx, attempt=attempt)
