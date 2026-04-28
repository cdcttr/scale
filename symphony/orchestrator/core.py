from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from symphony.config.schema import WorkflowConfig
from symphony.orchestrator.dispatch import is_eligible, sort_issues, retry_delay_ms
from symphony.orchestrator.state import (
    LiveSession, OrchestratorState, RetryEntry, TokenTotals,
)
from symphony.tracker.base import TrackerClient
from symphony.tracker.models import Issue
from symphony.worker.local import LocalWorker
from symphony.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: WorkflowConfig, tracker: TrackerClient) -> None:
        self._config = config
        self._tracker = tracker
        self._state = OrchestratorState()
        self._lock = asyncio.Lock()
        self._workspace = WorkspaceManager(config)
        self._refresh_event = asyncio.Event()

    def get_state(self) -> OrchestratorState:
        return self._state

    def request_refresh(self) -> None:
        self._refresh_event.set()

    async def run(self) -> None:
        await self._startup_cleanup()
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

    async def _tick(self) -> None:
        await self._reconcile()
        await self._fire_retries()
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
                if session is None:
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
                if not is_eligible(entry.issue, self._state, self._config):
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

    async def _run_worker(self, issue: Issue, attempt: Optional[int]) -> None:
        def on_event(event: dict) -> None:
            session = self._state.running.get(issue.id)
            if session:
                session.last_event_at = datetime.now(tz=timezone.utc)
                if event.get("type") == "result":
                    usage = event.get("usage", {})
                    session.tokens.input_tokens = usage.get("input_tokens", 0)
                    session.tokens.output_tokens = usage.get("output_tokens", 0)

        try:
            worker = LocalWorker(self._workspace, self._config)
            await worker.run(issue, self._config, attempt, on_event=on_event)
            async with self._lock:
                self._state.running.pop(issue.id, None)
                self._schedule_retry(issue, attempt=None, error="")
        except asyncio.CancelledError:
            async with self._lock:
                self._state.running.pop(issue.id, None)
            raise
        except Exception as e:
            logger.error(
                "issue_id=%s issue_identifier=%s worker failed: %s",
                issue.id, issue.identifier, e,
            )
            async with self._lock:
                self._state.running.pop(issue.id, None)
                current_attempt = (attempt or 0) + 1
                self._schedule_retry(issue, attempt=current_attempt, error=str(e))
