from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from scale.tracker.models import Issue


@dataclass
class TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LiveSession:
    issue: Issue
    task: asyncio.Task  # type: ignore[type-arg]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_event_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    turn_count: int = 0
    tokens: TokenTotals = field(default_factory=TokenTotals)
    session_id: str = ""
    finishing: bool = False


@dataclass
class RetryEntry:
    issue: Issue
    attempt: int
    due_at: datetime
    error: str


@dataclass
class CompletedSession:
    issue: Issue
    turn_count: int
    tokens: TokenTotals
    completed_at: datetime


@dataclass
class OrchestratorState:
    running: dict[str, LiveSession] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    retry_queue: list[RetryEntry] = field(default_factory=list)
    completed: list[CompletedSession] = field(default_factory=list)
    token_totals: TokenTotals = field(default_factory=TokenTotals)
    total_completed: int = 0
    pr_comment_watermarks: dict[int, datetime] = field(default_factory=dict)
