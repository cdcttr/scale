from __future__ import annotations
import logging
from datetime import datetime, timezone

from symphony.config.schema import TriageConfig
from symphony.tracker.github import GitHubClient
from symphony.tracker.models import Issue
from symphony.triage.agent import TriageAgent, TriageAssessment

log = logging.getLogger(__name__)

_MARKER_PREFIX = "<!-- symphony-triage "
_MARKER_SUFFIX = " -->"


def _parse_triage_timestamp(comment_body: str) -> datetime | None:
    if not comment_body.startswith(_MARKER_PREFIX):
        return None
    end = comment_body.find(_MARKER_SUFFIX, len(_MARKER_PREFIX))
    if end == -1:
        return None
    ts_str = comment_body[len(_MARKER_PREFIX):end]
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def _needs_triage(issue: Issue, comments: list[dict], force: bool) -> bool:
    if force:
        return True
    triage_comments = [c for c in comments if c["body"].startswith(_MARKER_PREFIX)]
    if not triage_comments:
        return True
    last_triage = max(
        triage_comments,
        key=lambda c: datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")),
    )
    ts = _parse_triage_timestamp(last_triage["body"])
    if ts is None or ts.tzinfo is None:
        return True
    issue_updated = issue.updated_at
    if issue_updated.tzinfo is None:
        issue_updated = issue_updated.replace(tzinfo=timezone.utc)
    return issue_updated > ts


def _build_comment_body(assessment: TriageAssessment) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return f"{_MARKER_PREFIX}{ts}{_MARKER_SUFFIX}\n{assessment.comment}"


class TriageRunner:
    def __init__(
        self,
        config: TriageConfig,
        client: GitHubClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._client = client
        self._agent = TriageAgent(config)
        self._dry_run = dry_run

    async def triage_issue(self, issue: Issue, force: bool = False) -> None:
        log.info("Checking issue #%d: %s", issue.number, issue.title)
        comments = await self._client.fetch_issue_comments(issue.number)

        if not _needs_triage(issue, comments, force):
            log.info("Issue #%d is already current, skipping", issue.number)
            return

        assessment = self._agent.assess(issue, comments)
        if assessment is None:
            log.warning("Skipping issue #%d due to assessment failure", issue.number)
            return

        status = "ready" if assessment.ready else "not ready"
        log.info("Issue #%d: %s — %s", issue.number, status, assessment.summary)

        if self._dry_run:
            print(f"\n--- Issue #{issue.number}: {issue.title} ---")
            print(f"Ready: {assessment.ready}")
            print(f"Summary: {assessment.summary}")
            if assessment.reasons:
                print(f"Reasons: {assessment.reasons}")
            print(f"Comment:\n{_build_comment_body(assessment)}")
            return

        body = _build_comment_body(assessment)
        await self._client.post_comment(issue.number, body)

        if assessment.ready:
            await self._client.add_labels(
                issue.number,
                [self._config.ready_label, self._config.triaged_label],
            )
            await self._client.remove_label(issue.number, self._config.needs_detail_label)
        else:
            await self._client.add_labels(
                issue.number,
                [self._config.needs_detail_label, self._config.triaged_label],
            )
            await self._client.remove_label(issue.number, self._config.ready_label)

    async def run(self, issues: list[Issue], force: bool = False) -> None:
        for issue in issues:
            await self.triage_issue(issue, force=force)
