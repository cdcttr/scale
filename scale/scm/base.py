from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


class SCMClient(ABC):
    @abstractmethod
    async def fetch_pr_for_branch(self, branch: str) -> Optional[dict]: ...

    @abstractmethod
    async def fetch_pr_for_issue(self, issue_number: int) -> Optional[dict]: ...

    @abstractmethod
    async def fetch_pr_diff(self, pr_number: int) -> str: ...

    @abstractmethod
    async def fetch_pr_checks(self, pr_number: int) -> list[dict]: ...

    @abstractmethod
    async def fetch_pr_comments(self, pr_number: int, since: Optional[datetime] = None) -> list[dict]: ...

    @abstractmethod
    async def fetch_conflict_context(self, branch_name: str) -> str: ...

    @abstractmethod
    async def merge_pr(self, pr_number: int) -> None: ...

    async def create_pr(self, head: str, base: str, title: str, body: str) -> dict:
        """Open a pull request; used by orchestrator-side publishing."""
        raise NotImplementedError(f"{type(self).__name__} does not support create_pr")
