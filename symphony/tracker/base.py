from __future__ import annotations
from abc import ABC, abstractmethod
from symphony.tracker.models import Issue


class TrackerClient(ABC):
    @abstractmethod
    async def fetch_candidate_issues(self) -> list[Issue]: ...

    @abstractmethod
    async def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]: ...

    @abstractmethod
    async def fetch_terminal_issues(self) -> list[Issue]: ...

    @abstractmethod
    async def fetch_issues_by_label(self, label: str) -> list[Issue]: ...
