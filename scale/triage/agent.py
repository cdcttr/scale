from __future__ import annotations
import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from scale.agent.claude import ClaudeRunner
from scale.config.schema import CodexConfig, TriageConfig
from scale.tracker.models import Issue

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a triage agent for an autonomous coding system called Scale.
Your job is to assess whether a GitHub issue is ready to be implemented autonomously.

An issue is READY if ALL of the following are true:
- The task is clearly stated (not a question or open-ended discussion)
- The scope is bounded — there is a defined "done" state
- Sufficient context is present to begin implementation without asking for clarification
- It is a coding/engineering task (not a process, policy, or design discussion)
- It does not require human approval (see NEEDS APPROVAL criteria below)

An issue NEEDS APPROVAL (well-specified but requires human review before dispatch) if ANY of the following apply:
- Large or unbounded scope (could touch many files or systems)
- Touches core orchestration, dispatch loop, or state management
- Multiple valid implementation approaches with significant trade-offs
- Destructive operations (deletions, schema changes, config rewrites)
- Ambiguous success criteria
- Changes to the prompt template or agent behavior

An issue is NOT READY (needs more detail) if ANY of the following apply:
- The title or body is vague (e.g., "fix the bug", "make it faster")
- Critical information is missing (which component, what behaviour, what the expected state is)
- The issue is a question or a discussion thread
- Multiple unrelated tasks are bundled together
- It depends on an external decision that has not been made

Respond with a JSON object only — no prose outside the JSON:
{
  "ready": true,
  "needs_approval": false,
  "summary": "One-sentence verdict",
  "reasons": ["Only populated if not ready or needs_approval — specific reasons"],
  "comment": "Full markdown comment body to post on GitHub"
}

Set ready=true and needs_approval=false for ready issues.
Set ready=false and needs_approval=true for issues that need human approval.
Set ready=false and needs_approval=false for issues that need more detail.

The comment field must follow exactly one of these formats:

Ready:
## Symphony Triage

**Status: Ready ✅**

This issue is clear and actionable. <one sentence explanation>

Needs approval:
## Symphony Triage

**Status: Needs Approval ⚠️**

This issue is well-specified but requires human review before autonomous dispatch:

- <specific reason>
- <specific reason>

A human should review and either approve dispatch or request more detail.

Not ready:
## Symphony Triage

**Status: Needs more detail ❌**

This issue needs clarification before it can be worked on autonomously:

- <specific gap>
- <specific gap>

Please add more detail and Symphony will re-evaluate when the issue is updated.\
"""


@dataclass
class TriageAssessment:
    ready: bool
    summary: str
    reasons: list[str] = field(default_factory=list)
    comment: str = ""
    needs_approval: bool = False


class TriageAgent:
    def __init__(self, config: TriageConfig, codex: CodexConfig) -> None:
        self._config = config
        self._runner = ClaudeRunner(codex)

    def _build_prompt(self, issue: Issue, comments: list[dict]) -> str:
        parts = [
            f"# Issue #{issue.number}: {issue.title}",
            "",
            "## Body",
            issue.description or "(no description)",
            "",
        ]
        if issue.labels:
            parts += ["## Labels", ", ".join(issue.labels), ""]
        if comments:
            parts += ["## Comments (newest last, up to 20)"]
            for c in comments[-20:]:
                author = c.get("user", {}).get("login", "unknown")
                parts.append(f"**{author}:** {c['body']}")
                parts.append("")
        parts += [
            "## Instructions",
            _SYSTEM_PROMPT,
        ]
        return "\n".join(parts)

    async def assess(
        self,
        issue: Issue,
        comments: list[dict],
        workspace: Path,
    ) -> TriageAssessment | None:
        prompt = self._build_prompt(issue, comments)
        try:
            result = await self._runner.run_turn(
                workspace=workspace,
                prompt=prompt,
                is_continuation=False,
                model=self._config.model,
            )
        except Exception as exc:
            log.error("Triage API call failed for issue #%d: %s", issue.number, exc)
            return None
        if not result.success:
            log.error("Triage call failed for issue #%d: %s", issue.number, result.message)
            return None
        try:
            text = result.message.strip()
            m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
            if m:
                text = m.group(1).strip()
            else:
                start, end = text.find("{"), text.rfind("}")
                if start != -1 and end != -1:
                    text = text[start:end + 1]
            data = json.loads(text)
            return TriageAssessment(
                ready=bool(data["ready"]),
                summary=data["summary"],
                reasons=data.get("reasons", []),
                comment=data.get("comment", ""),
                needs_approval=bool(data.get("needs_approval", False)),
            )
        except Exception as exc:
            log.error(
                "Triage JSON parse failed for issue #%d: %s\nRaw response: %s",
                issue.number, exc, result.message,
            )
            return None
