from __future__ import annotations
from abc import ABC, abstractmethod
from scale.tracker.models import Issue


class TrackerClient(ABC):
    @abstractmethod
    async def fetch_candidate_issues(self) -> list[Issue]: ...

    @abstractmethod
    async def fetch_open_issues(self) -> list[Issue]: ...

    @abstractmethod
    async def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]: ...

    @abstractmethod
    async def fetch_terminal_issues(self) -> list[Issue]: ...

    @abstractmethod
    async def fetch_issues_by_label(self, label: str) -> list[Issue]: ...

    @abstractmethod
    async def add_labels(self, number: int, labels: list[str]) -> None: ...

    @abstractmethod
    async def remove_label(self, number: int, label: str) -> None: ...

    @abstractmethod
    async def post_comment(self, number: int, body: str) -> None: ...

    @abstractmethod
    async def fetch_issue_comments(self, number: int) -> list[dict]: ...

    @abstractmethod
    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict: ...

    async def add_sub_issue(self, parent_number: int, child_node_id: str) -> bool:
        return False
