from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from scale.config.schema import WorkflowConfig
from scale.orchestrator.dispatch import is_eligible, sort_issues, retry_delay_ms
from scale.orchestrator.state import (
    CompletedSession, LiveSession, OrchestratorState, RetryEntry, SecondarySession, TokenTotals,
)
from scale.tracker.base import TrackerClient
from scale.tracker.github import GitHubClient
from scale.tracker.models import Issue
from scale.worker.feedback import FeedbackWorker
from scale.worker.local import LocalWorker
from scale.worker.review import ReviewWorker
from scale.worker.ssh import SSHWorker
from scale.workspace.manager import WorkspaceManager
from scale.planner.runner import PlannerRunner
from scale.triage.runner import TriageRunner

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: WorkflowConfig, tracker: TrackerClient) -> None:
        self._config = config
        self._tracker = tracker
        self._state = OrchestratorState()
        self._lock = asyncio.Lock()
        self._workspace = WorkspaceManager(config)
        self._refresh_event = asyncio.Event()
        self._ssh_index = 0
        self._github = GitHubClient(config.tracker)
        self._planner_runner = None
        if config.planner:
            self._planner_runner = PlannerRunner(config.planner, config.codex, self._github)
        self._triage_runner = None
        if config.triage:
            self._triage_runner = TriageRunner(config.triage, config.codex, self._github)

    def _has_slot(self, issue: Issue) -> bool:
        """Check concurrency limits only — not claimed/running status."""
        if len(self._state.running) >= self._config.agent.max_concurrent_agents:
            return False
        for state_name, limit in self._config.agent.max_concurrent_agents_by_state.items():
            if issue.state == state_name:
                count = sum(
                    1 for s in self._state.running.values()
                    if s.issue.state == state_name
                )
                if count >= limit:
                    return False
        return True

    def _make_worker(self) -> LocalWorker | SSHWorker:
        hosts = self._config.worker.ssh_hosts
        if hosts:
            host = hosts[self._ssh_index % len(hosts)]
            self._ssh_index += 1
            return SSHWorker(host, self._workspace, self._config)
        return LocalWorker(self._workspace, self._config)

    def get_state(self) -> OrchestratorState:
        return self._state

    def request_refresh(self) -> None:
        self._refresh_event.set()

    async def _gh_add_labels(self, number: int, labels: list[str]) -> None:
        if self._github:
            await self._github.add_labels(number, labels)

    async def _gh_remove_label(self, number: int, label: str) -> None:
        if self._github:
            await self._github.remove_label(number, label)

    async def run(self) -> None:
        await self._startup_cleanup()
        tasks = [asyncio.create_task(self._tick_loop())]
        if self._config.planner:
            tasks.append(asyncio.create_task(self._watch_planned()))
        if self._config.review:
            tasks.append(asyncio.create_task(self._watch_merge_queue()))
        if self._config.review and self._config.review.feedback_enabled:
            tasks.append(asyncio.create_task(self._watch_pr_feedback()))
        await asyncio.gather(*tasks)

    async def _tick_loop(self) -> None:
        while True:
            self._refresh_event.clear()
            await self._tick()
            try:
                await asyncio.wait_for(
                    self._refresh_event.wait(),
                    timeout=self._config.polling.interval_ms / 1000,
                )
            except asyncio.TimeoutError:
                pass

    async def _startup_cleanup(self) -> None:
        try:
            terminal = await self._tracker.fetch_terminal_issues()
            for issue in terminal:
                await self._workspace.remove(issue, hooks_enabled=False)
        except Exception as e:
            logger.warning("Startup terminal cleanup failed (continuing): %s", e)

    async def _expire_completed(self) -> None:
        ttl_s = self._config.agent.completed_display_s
        cutoff = datetime.now(tz=timezone.utc).timestamp() - ttl_s
        async with self._lock:
            self._state.completed = [
                c for c in self._state.completed
                if c.completed_at.timestamp() > cutoff
            ]

    async def _tick(self) -> None:
        logger.info(
            "tick: running=%d retries=%d completed=%d",
            len(self._state.running),
            len(self._state.retry_queue),
            self._state.total_completed,
        )
        await self._flush_finishing()
        await self._expire_completed()
        await self._reconcile()
        await self._fire_retries()
        if self._config.triage and self._triage_runner:
            try:
                triage_label = self._config.triage.triage_label
                triage_exclusion = {
                    self._config.triage.triaged_label,
                    self._config.triage.ready_label,
                    self._config.triage.needs_detail_label,
                    self._config.triage.needs_approval_label,
                    self._config.agent.supervised_label,
                    *self._config.tracker.skip_labels,
                    *self._config.tracker.terminal_labels,
                }
                all_open = await self._github.fetch_open_issues()
                async with self._lock:
                    for issue in all_open:
                        if issue.id in self._state.claimed:
                            continue
                        if triage_label not in issue.labels:
                            continue
                        if any(label in triage_exclusion for label in issue.labels):
                            continue
                        self._state.claimed.add(issue.id)
                        asyncio.create_task(self._run_triage(issue))
            except Exception as e:
                logger.warning("Triage issue fetch failed: %s", e)
        if self._config.planner and self._planner_runner:
            try:
                plan_issues = await self._tracker.fetch_issues_by_label(
                    self._config.planner.plan_label
                )
                async with self._lock:
                    for issue in plan_issues:
                        if issue.id not in self._state.claimed:
                            self._state.claimed.add(issue.id)
                            asyncio.create_task(self._run_planner(issue))
            except Exception as e:
                logger.warning("Plan issue fetch failed: %s", e)
        if self._config.review:
            try:
                pr_open_issues = await self._github.fetch_issues_by_label(
                    self._config.review.pr_open_label
                )
                async with self._lock:
                    for issue in pr_open_issues:
                        if issue.id not in self._state.claimed:
                            if self._config.agent.supervised_label in (issue.labels or []):
                                continue
                            self._state.claimed.add(issue.id)
                            asyncio.create_task(self._run_reviewer(issue))
            except Exception as e:
                logger.warning("Review dispatch failed: %s", e)
        try:
            issues = await self._tracker.fetch_candidate_issues()
        except Exception as e:
            logger.warning("Candidate fetch failed, skipping dispatch: %s", e)
            return
        sorted_issues = sort_issues(issues)
        async with self._lock:
            for issue in sorted_issues:
                if not is_eligible(issue, self._state, self._config):
                    continue
                self._state.claimed.add(issue.id)
                task = asyncio.create_task(self._run_worker(issue, attempt=None))
                self._state.running[issue.id] = LiveSession(issue=issue, task=task)

    async def _reconcile(self) -> None:
        async with self._lock:
            running_numbers = [
                s.issue.number for s in self._state.running.values()
                if not s.finishing
            ]

        if not running_numbers:
            return

        try:
            refreshed = await self._tracker.fetch_issues_by_numbers(running_numbers)
            refreshed_by_id = {i.id: i for i in refreshed}
        except Exception as e:
            logger.warning("State refresh failed, keeping workers running: %s", e)
            return

        now = datetime.now(tz=timezone.utc).timestamp()
        stall_s = self._config.codex.stall_timeout_ms / 1000

        async with self._lock:
            for issue_id in list(self._state.running.keys()):
                session = self._state.running.get(issue_id)
                if session is None or session.finishing:
                    continue

                elapsed = now - session.last_event_at.timestamp()
                if stall_s > 0 and elapsed > stall_s:
                    logger.warning(
                        "issue_id=%s stall detected after %.0fs, cancelling",
                        issue_id, elapsed,
                    )
                    session.task.cancel()
                    self._schedule_retry(session.issue, attempt=1, error="stall timeout")
                    continue

                refreshed_issue = refreshed_by_id.get(issue_id)
                if refreshed_issue is None:
                    session.task.cancel()
                    continue
                if refreshed_issue.state == "terminal":
                    session.task.cancel()
                    asyncio.create_task(self._workspace.remove(refreshed_issue))

    def _schedule_retry(
        self,
        issue: Issue,
        attempt: Optional[int],
        error: str,
    ) -> None:
        delay_ms = retry_delay_ms(attempt, self._config.agent.max_retry_backoff_ms)
        due_at = datetime.now(tz=timezone.utc).timestamp() + delay_ms / 1000
        entry = RetryEntry(
            issue=issue,
            attempt=(attempt or 0) + 1,
            due_at=datetime.fromtimestamp(due_at, tz=timezone.utc),
            error=error,
        )
        self._state.retry_queue.append(entry)
        self._state.retry_queue.sort(key=lambda e: e.due_at)
        logger.info(
            "issue_id=%s scheduled retry attempt=%d delay_ms=%d reason=%s",
            issue.id, entry.attempt, delay_ms, error,
        )

    async def _fire_retries(self) -> None:
        now = datetime.now(tz=timezone.utc)
        async with self._lock:
            due = [e for e in self._state.retry_queue if e.due_at <= now]
            self._state.retry_queue = [
                e for e in self._state.retry_queue if e.due_at > now
            ]
        for entry in due:
            try:
                issues = await self._tracker.fetch_issues_by_numbers([entry.issue.number])
                if not issues or issues[0].state != "active":
                    async with self._lock:
                        self._state.claimed.discard(entry.issue.id)
                    continue
                if not self._has_slot(entry.issue):
                    self._schedule_retry(entry.issue, entry.attempt, "no slots")
                    continue
                async with self._lock:
                    task = asyncio.create_task(
                        self._run_worker(entry.issue, attempt=entry.attempt)
                    )
                    self._state.running[entry.issue.id] = LiveSession(
                        issue=entry.issue, task=task
                    )
            except Exception as e:
                logger.warning("Retry fire failed for %s: %s", entry.issue.id, e)
                self._schedule_retry(entry.issue, entry.attempt, str(e))

    async def _flush_finishing(self) -> None:
        async with self._lock:
            for issue_id in list(self._state.running.keys()):
                session = self._state.running.get(issue_id)
                if session and session.finishing:
                    self._state.running.pop(issue_id)
                    self._state.token_totals.input_tokens += session.tokens.input_tokens
                    self._state.token_totals.output_tokens += session.tokens.output_tokens
                    self._state.completed.append(CompletedSession(
                        issue=session.issue,
                        turn_count=session.turn_count,
                        tokens=session.tokens,
                        completed_at=datetime.now(tz=timezone.utc),
                    ))
                    self._state.total_completed += 1

    async def _collect_workspace_state(self, workspace_path: Path) -> dict:
        result: dict = {"modified_files": [], "new_files": [], "commits": []}
        if not workspace_path.exists():
            return result

        async def _git(*args: str) -> str:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "git", *args,
                    cwd=str(workspace_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                return stdout.decode().strip()
            except Exception:
                return ""

        diff = await _git("diff", "--name-only", "origin/main..HEAD")
        if diff:
            result["modified_files"] = [f for f in diff.splitlines() if f]

        status = await _git("status", "--porcelain")
        if status:
            result["new_files"] = [
                line[3:] for line in status.splitlines() if line.startswith("?? ")
            ]

        log = await _git("log", "--oneline", "origin/main..HEAD")
        if log:
            result["commits"] = [l for l in log.splitlines() if l]

        return result

    async def _post_attempt_summary(
        self,
        issue: Issue,
        session: LiveSession,
        attempt: Optional[int],
    ) -> None:
        workspace_path = self._workspace.path(issue)
        try:
            state = await self._collect_workspace_state(workspace_path)
        except Exception as exc:
            logger.warning("Failed to collect workspace state for summary: %s", exc)
            state = {"modified_files": [], "new_files": [], "commits": []}

        attempt_num = (attempt or 0) + 1

        def _fmt(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        lines = [
            "<!-- scale-attempt-summary -->",
            "",
            f"## Scale attempt {attempt_num} summary",
            "",
            f"- **Turns completed:** {session.turn_count}",
            f"- **Tokens in:** {_fmt(session.tokens.input_tokens)}"
            f"  |  **Tokens out:** {_fmt(session.tokens.output_tokens)}",
            "",
        ]

        if state["commits"]:
            lines += ["### Commits", "```"]
            lines.extend(state["commits"])
            lines += ["```", ""]
        else:
            lines += ["### Commits", "_No commits made._", ""]

        if state["modified_files"]:
            lines += ["### Files modified"]
            for f in state["modified_files"]:
                lines.append(f"- `{f}`")
            lines.append("")

        if state["new_files"]:
            lines += ["### Files created (untracked)"]
            for f in state["new_files"]:
                lines.append(f"- `{f}`")
            lines.append("")

        if not state["modified_files"] and not state["new_files"] and not state["commits"]:
            lines += ["_No file changes detected._", ""]

        comment = "\n".join(lines)
        try:
            await self._github.post_comment(issue.number, comment)
        except Exception as exc:
            logger.warning(
                "Failed to post attempt summary for issue #%d: %s", issue.number, exc
            )

    async def _fetch_previous_attempt_summary(self, issue: Issue) -> Optional[str]:
        try:
            comments = await self._github.fetch_issue_comments(issue.number)
            for comment in reversed(comments):
                if "<!-- scale-attempt-summary -->" in comment.get("body", ""):
                    return comment["body"]
        except Exception as exc:
            logger.warning(
                "Failed to fetch previous attempt summary for issue #%d: %s",
                issue.number, exc,
            )
        return None

    async def _run_worker(self, issue: Issue, attempt: Optional[int]) -> None:
        previous_attempt_summary: Optional[str] = None
        if attempt:
            previous_attempt_summary = await self._fetch_previous_attempt_summary(issue)

        def on_event(event: dict) -> None:
            session = self._state.running.get(issue.id)
            if session:
                session.last_event_at = datetime.now(tz=timezone.utc)
                if event.get("type") == "assistant":
                    session.turn_count += 1
                    usage = (event.get("message") or {}).get("usage") or {}
                    session.tokens.input_tokens += (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
                    session.tokens.output_tokens += (
                        usage.get("output_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                    )
                elif event.get("type") == "scale:stall":
                    session.stall_info = {
                        "elapsed_s": event.get("elapsed_s"),
                        "uncommitted_files": event.get("uncommitted_files"),
                        "commits_since_start": event.get("commits_since_start"),
                        "status_summary": event.get("status_summary"),
                        "grace_period": event.get("grace_period"),
                    }

        try:
            worker = self._make_worker()
            await worker.run(
                issue, self._config, attempt,
                on_event=on_event,
                previous_attempt_summary=previous_attempt_summary,
            )
            async with self._lock:
                session = self._state.running.get(issue.id)
                if session:
                    session.finishing = True
                self._state.claimed.discard(issue.id)
            if session:
                await self._record_stats(issue, session, success=True, attempt=attempt)
            is_supervised = self._config.agent.supervised_label in (issue.labels or [])
            if self._config.review:
                await self._github.add_labels(issue.number, [self._config.review.pr_open_label])
            elif self._config.agent.auto_merge and not is_supervised:
                pr = await self._github.fetch_pr_for_branch(issue.branch_name)
                if pr is not None:
                    await self._try_auto_merge(issue, pr["number"])
                if self._config.tracker.terminal_labels:
                    await self._github.add_labels(issue.number, [self._config.tracker.terminal_labels[0]])
            elif self._config.tracker.terminal_labels:
                await self._github.add_labels(issue.number, [self._config.tracker.terminal_labels[0]])
            asyncio.create_task(self._workspace.remove(issue))
        except asyncio.CancelledError:
            async with self._lock:
                session = self._state.running.pop(issue.id, None)
                if session:
                    self._state.token_totals.input_tokens += session.tokens.input_tokens
                    self._state.token_totals.output_tokens += session.tokens.output_tokens
            if session:
                await self._post_attempt_summary(issue, session, attempt)
            raise
        except Exception as e:
            logger.error(
                "issue_id=%s issue_identifier=%s worker failed: %s",
                issue.id, issue.identifier, e,
            )
            async with self._lock:
                session = self._state.running.pop(issue.id, None)
                if session:
                    self._state.token_totals.input_tokens += session.tokens.input_tokens
                    self._state.token_totals.output_tokens += session.tokens.output_tokens
                current_attempt = (attempt or 0) + 1
                self._schedule_retry(issue, attempt=current_attempt, error=str(e))
            if session:
                await self._record_stats(issue, session, success=False, attempt=attempt)
                await self._post_attempt_summary(issue, session, attempt)

    async def _record_stats(
        self,
        issue: "Issue",
        session: "LiveSession",
        success: bool,
        attempt: "Optional[int]",
    ) -> None:
        now = datetime.now(timezone.utc)
        duration_s = round((now - session.started_at).total_seconds())
        attempt_num = (attempt or 0) + 1
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        stats: dict = {
            "issue": issue.number,
            "turns": session.turn_count,
            "input_tokens": session.tokens.input_tokens,
            "output_tokens": session.tokens.output_tokens,
            "duration_s": duration_s,
            "attempt": attempt_num,
            "success": success,
            "timestamp": timestamp,
        }
        if session.stall_info is not None:
            stats["stall"] = session.stall_info

        def _fmt_tokens(n: int) -> str:
            return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

        def _fmt_duration(s: int) -> str:
            return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

        marker = f"<!-- scale-stats {json.dumps(stats)} -->"
        comment = (
            f"{marker}\n\n"
            "## Scale run complete\n\n"
            f"- **Turns:** {stats['turns']}\n"
            f"- **Tokens in:** {_fmt_tokens(stats['input_tokens'])}"
            f"  |  **Tokens out:** {_fmt_tokens(stats['output_tokens'])}\n"
            f"- **Duration:** {_fmt_duration(duration_s)}\n"
            f"- **Attempt:** {attempt_num}"
        )

        try:
            await self._github.post_comment(issue.number, comment)
        except Exception as exc:
            logger.warning("Failed to post stats comment for issue #%d: %s", issue.number, exc)

        jsonl_record = {**stats, "issue_title": issue.title}
        try:
            with Path("stats.jsonl").open("a") as fh:
                fh.write(json.dumps(jsonl_record) + "\n")
        except Exception as exc:
            logger.warning("Failed to write stats.jsonl: %s", exc)

    async def _run_planner(self, issue: Issue) -> None:
        try:
            await self._planner_runner.plan_issue(issue)
        except Exception as e:
            logger.error("Planner failed for issue #%d: %s", issue.number, e)
        finally:
            async with self._lock:
                self._state.claimed.discard(issue.id)

    async def _run_triage(self, issue: Issue) -> None:
        try:
            await self._triage_runner.triage_issue(issue)
        except Exception as e:
            logger.error("Triage failed for issue #%d: %s", issue.number, e)
        finally:
            async with self._lock:
                self._state.claimed.discard(issue.id)

    async def _run_reviewer(self, issue: Issue) -> None:
        assert self._config.review is not None
        review = self._config.review
        async with self._lock:
            self._state.secondary[issue.id] = SecondarySession(issue=issue, kind="review")
        try:
            pr = await self._github.fetch_pr_for_branch(issue.branch_name)
            if pr is None:
                pr = await self._github.fetch_pr_for_issue(issue.number)
            if pr is None:
                logger.warning("No open PR found for issue #%d, skipping review", issue.number)
                return
            pr_number: int = pr["number"]
            pr_url: str = pr["html_url"]
            pr_diff = await self._github.fetch_pr_diff(pr_number)
            worker = ReviewWorker(self._config)
            await worker.run(issue, pr_number=pr_number, pr_url=pr_url, pr_diff=pr_diff)
            if self._config.agent.auto_merge:
                await self._try_auto_merge(issue, pr_number)
            if self._config.tracker.terminal_labels:
                await self._github.add_labels(
                    issue.number, [self._config.tracker.terminal_labels[0]]
                )
            await self._github.remove_label(issue.number, review.pr_open_label)
        except Exception as e:
            logger.error("Reviewer failed for issue #%d: %s", issue.number, e)
            await self._github.add_labels(issue.number, [review.conflict_label])
            await self._github.remove_label(issue.number, review.pr_open_label)
        finally:
            async with self._lock:
                self._state.secondary.pop(issue.id, None)
                self._state.claimed.discard(issue.id)

    async def _watch_planned(self) -> None:
        while True:
            try:
                await self._watch_planned_tick()
            except Exception as e:
                logger.warning("watch_planned tick failed: %s", e)
            await asyncio.sleep(self._config.polling.interval_ms / 1000)

    async def _watch_pr_feedback(self) -> None:
        while True:
            try:
                await self._watch_pr_feedback_tick()
            except Exception as e:
                logger.warning("watch_pr_feedback tick failed: %s", e)
            await asyncio.sleep(self._config.polling.interval_ms / 1000)

    async def _watch_pr_feedback_tick(self) -> None:
        assert self._config.review is not None
        review = self._config.review

        pr_open_issues = await self._github.fetch_issues_by_label(review.pr_open_label)

        for issue in pr_open_issues:
            async with self._lock:
                if issue.id in self._state.claimed:
                    continue

            if issue.number not in self._state.pr_comment_watermarks:
                self._state.pr_comment_watermarks[issue.number] = datetime.now(tz=timezone.utc)
                continue

            watermark = self._state.pr_comment_watermarks[issue.number]
            try:
                comments = await self._github.fetch_pr_comments(issue.number, since=watermark)
            except Exception as e:
                logger.warning("Failed to fetch PR comments for issue #%d: %s", issue.number, e)
                continue

            human_comments = [
                c for c in comments
                if "<!-- scale-stats" not in c.get("body", "")
            ]

            if not human_comments:
                continue

            async with self._lock:
                self._state.claimed.add(issue.id)
            asyncio.create_task(self._run_feedback_worker(issue, human_comments))

    async def _run_feedback_worker(self, issue: Issue, comments: list[dict]) -> None:
        async with self._lock:
            self._state.secondary[issue.id] = SecondarySession(issue=issue, kind="feedback")
        try:
            pr = await self._github.fetch_pr_for_branch(issue.branch_name)
            if pr is None:
                logger.warning("No open PR found for issue #%d, skipping feedback", issue.number)
                return
            pr_diff = await self._github.fetch_pr_diff(pr["number"])
            worker = FeedbackWorker(self._workspace, self._config)
            await worker.run(issue, pr_diff=pr_diff, pr_comments=comments)
            logger.info("Feedback worker completed for issue #%d", issue.number)
        except Exception as e:
            logger.error("Feedback worker failed for issue #%d: %s", issue.number, e)
        finally:
            self._state.pr_comment_watermarks[issue.number] = datetime.now(tz=timezone.utc)
            async with self._lock:
                self._state.secondary.pop(issue.id, None)
            self._state.claimed.discard(issue.id)

    async def _try_auto_merge(self, issue: Issue, pr_number: int) -> None:
        for _ in range(10):
            checks = await self._github.fetch_pr_checks(pr_number)
            if not checks:
                await self._github.merge_pr(pr_number)
                return
            all_done = all(c.get("status") == "completed" for c in checks)
            if all_done:
                all_pass = all(
                    c.get("conclusion") in ("success", "skipped", "neutral")
                    for c in checks
                )
                if all_pass:
                    await self._github.merge_pr(pr_number)
                else:
                    logger.warning(
                        "CI checks failed for PR #%d on issue #%d, leaving PR open",
                        pr_number, issue.number,
                    )
                return
            await asyncio.sleep(30)
        logger.warning(
            "CI checks timed out for PR #%d on issue #%d, leaving PR open",
            pr_number, issue.number,
        )

    async def _watch_merge_queue(self) -> None:
        while True:
            try:
                await self._watch_merge_queue_tick()
            except Exception as e:
                logger.warning("watch_merge_queue tick failed: %s", e)
            await asyncio.sleep(self._config.polling.interval_ms / 1000)

    async def _watch_merge_queue_tick(self) -> None:
        if not self._config.review:
            return
        review = self._config.review
        pr_open_issues = await self._github.fetch_issues_by_label(review.pr_open_label)
        async with self._lock:
            candidates = []
            for issue in pr_open_issues:
                if review.merge_label not in (issue.labels or []):
                    continue
                if issue.id in self._state.claimed:
                    continue
                self._state.claimed.add(issue.id)
                candidates.append(issue)
        for issue in candidates:
            asyncio.create_task(self._merge_issue(issue))

    async def _merge_issue(self, issue: Issue) -> None:
        assert self._config.review is not None
        review = self._config.review
        try:
            pr = await self._github.fetch_pr_for_branch(issue.branch_name)
            if pr is None:
                logger.warning("No open PR for issue #%d, skipping merge", issue.number)
                return
            await self._github.merge_pr(pr["number"])
            if self._config.tracker.terminal_labels:
                await self._github.add_labels(
                    issue.number, [self._config.tracker.terminal_labels[0]]
                )
            await self._github.remove_label(issue.number, review.pr_open_label)
            await self._github.remove_label(issue.number, review.merge_label)
        except Exception as e:
            logger.error("Merge failed for issue #%d: %s", issue.number, e)
        finally:
            async with self._lock:
                self._state.claimed.discard(issue.id)

    async def _watch_planned_tick(self) -> None:
        if not self._config.planner or not self._planner_runner:
            return
        planned_issues = await self._tracker.fetch_issues_by_label(
            self._config.planner.planned_label
        )
        for issue in planned_issues:
            child_numbers = await self._planner_runner.get_child_numbers(issue)
            if not child_numbers:
                continue
            children = await self._tracker.fetch_issues_by_numbers(child_numbers)
            if not children:
                continue
            all_terminal = all(c.state == "terminal" for c in children)
            if all_terminal:
                logger.info(
                    "All children of issue #%d complete, closing parent", issue.number
                )
                if not self._config.tracker.terminal_labels:
                    logger.warning("Cannot close planned parent issues: terminal_labels is empty in tracker config")
                    continue
                terminal_label = self._config.tracker.terminal_labels[0]
                await self._gh_add_labels(issue.number, [terminal_label])
                await self._gh_remove_label(issue.number, self._config.planner.planned_label)
