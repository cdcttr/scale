from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from scale.config.schema import CodexConfig, PlannerConfig
from scale.tracker.base import TrackerClient
from scale.tracker.models import Issue
from scale.planner.agent import PlannerAgent

log = logging.getLogger(__name__)

_MARKER_PREFIX = "<!-- scale-plan "
_MARKER_SUFFIX = " -->"


def _parse_plan_marker(body: str) -> Optional[dict]:
    if not body.startswith(_MARKER_PREFIX):
        return None
    end = body.find(_MARKER_SUFFIX, len(_MARKER_PREFIX))
    if end == -1:
        return None
    try:
        return json.loads(body[len(_MARKER_PREFIX):end])
    except Exception:
        return None


def _build_marker(children: list[int], depth: int) -> str:
    data = json.dumps({"children": children, "depth": depth})
    return f"{_MARKER_PREFIX}{data}{_MARKER_SUFFIX}"


def _get_depth(issue: Issue) -> int:
    depths = []
    for label in issue.labels:
        if label.startswith("scale:depth:"):
            try:
                depths.append(int(label.split(":")[-1]))
            except ValueError:
                pass
    return max(depths) if depths else 0


class PlannerRunner:
    def __init__(
        self,
        config: PlannerConfig,
        codex: CodexConfig,
        client: TrackerClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._client = client
        self._agent = PlannerAgent(config, codex)
        self._dry_run = dry_run
        self._workspace = Path(config.planner_workspace)
        self._sub_issues_available: Optional[bool] = None

    async def _try_add_sub_issue(self, parent_number: int, child_node_id: str) -> None:
        if self._sub_issues_available is False:
            return
        result = await self._client.add_sub_issue(parent_number, child_node_id)
        if not result:
            self._sub_issues_available = False

    async def plan_issue(self, issue: Issue, force: bool = False) -> None:
        if self._config.planned_label in issue.labels and not force:
            log.info("Issue #%d already planned, skipping (use --force to re-plan)", issue.number)
            return

        depth = _get_depth(issue)
        self._workspace.mkdir(parents=True, exist_ok=True)

        comments = await self._client.fetch_issue_comments(issue.number)
        assessment = await self._agent.assess(issue, comments, depth, self._workspace)

        if assessment is None:
            log.warning("Skipping issue #%d due to assessment failure", issue.number)
            return

        if assessment.is_leaf:
            log.info("Issue #%d classified as leaf task", issue.number)
            if self._dry_run:
                print(f"\n--- Issue #{issue.number}: {issue.title} ---")
                print("Type: leaf")
                return
            await self._client.add_labels(issue.number, [self._config.leaf_label])
            await self._client.remove_label(issue.number, self._config.plan_label)
            return

        log.info(
            "Issue #%d classified as concept, decomposing into %d children",
            issue.number, len(assessment.children),
        )

        if self._dry_run:
            print(f"\n--- Issue #{issue.number}: {issue.title} ---")
            print("Type: concept")
            for i, child in enumerate(assessment.children):
                print(f"\nChild {i + 1}: {child.title}")
                print(f"Labels: {child.labels}")
                print(f"Description:\n{child.description}")
            return

        child_numbers: list[int] = []
        child_depth = depth + 1
        depth_label = f"scale:depth:{child_depth}"

        try:
            for child_spec in assessment.children:
                child_labels = list(child_spec.labels) + [depth_label]
                child_issue = await self._client.create_issue(
                    title=child_spec.title,
                    body=f"_Decomposed from #{issue.number}_\n\n{child_spec.description}",
                    labels=child_labels,
                )
                child_numbers.append(child_issue["number"])
                await self._try_add_sub_issue(issue.number, child_issue["node_id"])
        except Exception:
            if child_numbers:
                log.warning(
                    "Partial child creation for issue #%d (%d/%d created), posting partial marker",
                    issue.number, len(child_numbers), len(assessment.children),
                )
                await self._client.post_comment(issue.number, _build_marker(child_numbers, depth))
            raise

        marker = _build_marker(child_numbers, depth)
        await self._client.post_comment(issue.number, marker)
        await self._client.add_labels(
            issue.number,
            [self._config.concept_label, self._config.planned_label],
        )
        await self._client.remove_label(issue.number, self._config.plan_label)

    async def get_child_numbers(self, issue: Issue) -> list[int]:
        comments = await self._client.fetch_issue_comments(issue.number)
        for comment in reversed(comments):
            data = _parse_plan_marker(comment["body"])
            if data is not None:
                return data.get("children", [])
        return []

    async def run(self, issues: list[Issue], force: bool = False) -> None:
        for issue in issues:
            await self.plan_issue(issue, force=force)
