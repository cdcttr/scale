from __future__ import annotations
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from scale.agent.claude import ClaudeRunner
from scale.config.schema import CodexConfig, PlannerConfig
from scale.tracker.models import Issue

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a planning agent for an autonomous coding system called Scale.
Your job is to assess whether a GitHub issue can be implemented directly as a
leaf task, or needs to be broken into smaller child issues first.

A LEAF task is directly implementable when:
- The scope is clearly bounded with a defined "done" state
- An agent can complete it in one focused session
- No coordination with other parallel tasks is required

A CONCEPT needs decomposition when:
- It spans multiple independent components or concerns
- It can be clearly split into independently implementable subtasks

When decomposing, each child must be independently implementable as a leaf task.
Include enough context in each child description for an agent to implement it
without access to the parent issue.

You have access to the repository via your tools. Browse the codebase to inform
your decomposition decisions where helpful.

Respond with a JSON object only — no prose outside the JSON.

For a leaf task:
{"type": "leaf", "children": null}

For a concept:
{
  "type": "concept",
  "children": [
    {
      "title": "Concise imperative title",
      "description": "Full markdown description with acceptance criteria",
      "labels": ["symphony:ready"]
    }
  ]
}\
"""


@dataclass
class ChildSpec:
    title: str
    description: str
    labels: list[str] = field(default_factory=list)


@dataclass
class PlanAssessment:
    is_leaf: bool
    children: list[ChildSpec] = field(default_factory=list)


class PlannerAgent:
    def __init__(self, config: PlannerConfig, codex: CodexConfig) -> None:
        self._config = config
        self._runner = ClaudeRunner(codex)

    def _build_prompt(self, issue: Issue, comments: list[dict], depth: int) -> str:
        parts = [
            f"# Issue #{issue.number}: {issue.title}",
            f"Current depth: {depth} (max allowed: {self._config.max_depth - 1})",
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
        parts += ["## Instructions", _SYSTEM_PROMPT]
        return "\n".join(parts)

    async def assess(
        self,
        issue: Issue,
        comments: list[dict],
        depth: int,
        workspace: Path,
    ) -> PlanAssessment | None:
        if depth >= self._config.max_depth:
            return PlanAssessment(is_leaf=True)

        prompt = self._build_prompt(issue, comments, depth)
        try:
            result = await self._runner.run_turn(
                workspace=workspace,
                prompt=prompt,
                is_continuation=False,
                model=self._config.model,
            )
        except Exception as exc:
            log.error("Planning API call failed for issue #%d: %s", issue.number, exc)
            return None

        if not result.success:
            log.error("Planning call failed for issue #%d: %s", issue.number, result.message)
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
            ptype = data.get("type")
            if ptype == "leaf":
                return PlanAssessment(is_leaf=True)
            if ptype != "concept":
                raise ValueError(f"Unknown plan type: {ptype!r}")
            raw_children = data.get("children") or []
            if not raw_children:
                raise ValueError("concept response had no children")
            children = [
                ChildSpec(
                    title=c["title"],
                    description=c["description"],
                    labels=c.get("labels", []),
                )
                for c in raw_children
            ]
            return PlanAssessment(is_leaf=False, children=children)
        except Exception as exc:
            log.error(
                "Planning JSON parse failed for issue #%d: %s\nRaw: %s",
                issue.number, exc, result.message,
            )
            return None
