from __future__ import annotations
import re
import asyncio
from datetime import datetime
from typing import Optional
from urllib.parse import quote as _url_quote

import httpx

from scale.tracker.base import TrackerClient
from scale.tracker.models import Issue
from scale.config.schema import TrackerConfig

_PRIORITY_RE = re.compile(r'^priority:(\d+)$')
_SLUG_RE = re.compile(r'[^a-z0-9]+')


def _slugify(text: str) -> str:
    return _SLUG_RE.sub('-', text.lower()).strip('-')[:50]


def _parse_priority(labels: list[str]) -> Optional[int]:
    for label in labels:
        m = _PRIORITY_RE.match(label)
        if m:
            return int(m.group(1))
    return None


class GitHubClient(TrackerClient):
    def __init__(self, config: TrackerConfig) -> None:
        self._config = config
        owner, repo = config.repo.split('/', 1)
        self._owner = owner
        self._base = f"https://api.github.com/repos/{owner}/{repo}"
        self._headers = {
            "Authorization": f"Bearer {config.api_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _resolve_state(self, labels: list[str], github_state: str) -> str:
        label_set = set(labels)
        if github_state == "closed":
            return "terminal"
        if label_set & set(self._config.terminal_labels):
            return "terminal"
        if label_set & set(self._config.skip_labels):
            return "ignored"
        if self._config.active_labels:
            if not all(l in label_set for l in self._config.active_labels):
                return "ignored"
        return "active"

    def _normalize(self, item: dict) -> Issue:
        labels = [l["name"] for l in item.get("labels", [])]
        number = item["number"]
        title = item["title"]
        return Issue(
            id=str(item["node_id"]),
            identifier=f"{self._config.repo}#{number}",
            number=number,
            title=title,
            description=item.get("body") or "",
            state=self._resolve_state(labels, item["state"]),
            labels=labels,
            branch_name=f"symphony/{number}-{_slugify(title)}",
            url=item["html_url"],
            priority=_parse_priority(labels),
            created_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
            updated_at=datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")),
        )

    async def _paginate(self, client: httpx.AsyncClient, params: dict) -> list[dict]:
        results = []
        page = 1
        while True:
            r = await client.get(
                f"{self._base}/issues",
                headers=self._headers,
                params={**params, "per_page": 100, "page": page},
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            results.extend(data)
            page += 1
        return results

    async def fetch_candidate_issues(self) -> list[Issue]:
        async with httpx.AsyncClient(timeout=30) as client:
            items = await self._paginate(client, {"state": "open"})
        issues = []
        for item in items:
            if "pull_request" in item:
                continue
            normalized = self._normalize(item)
            if normalized.state == "active":
                issues.append(normalized)
        return issues

    async def fetch_open_issues(self) -> list[Issue]:
        async with httpx.AsyncClient(timeout=30) as client:
            items = await self._paginate(client, {"state": "open"})
        return [
            self._normalize(item)
            for item in items
            if "pull_request" not in item
        ]

    async def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]:
        async def _fetch(n: int) -> Optional[Issue]:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(
                    f"{self._base}/issues/{n}", headers=self._headers
                )
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return self._normalize(r.json())

        results = await asyncio.gather(*[_fetch(n) for n in numbers], return_exceptions=True)
        return [r for r in results if isinstance(r, Issue)]

    async def fetch_terminal_issues(self) -> list[Issue]:
        async with httpx.AsyncClient(timeout=30) as client:
            items = await self._paginate(client, {"state": "closed"})
        return [
            self._normalize(item)
            for item in items
            if "pull_request" not in item
        ]

    async def fetch_issue_comments(self, number: int) -> list[dict]:
        results: list[dict] = []
        page = 1
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                r = await client.get(
                    f"{self._base}/issues/{number}/comments",
                    headers=self._headers,
                    params={"per_page": 100, "page": page},
                )
                r.raise_for_status()
                data = r.json()
                if not data:
                    break
                results.extend(data)
                page += 1
        return results

    async def post_comment(self, number: int, body: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base}/issues/{number}/comments",
                headers=self._headers,
                json={"body": body},
            )
            r.raise_for_status()

    async def add_labels(self, number: int, labels: list[str]) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base}/issues/{number}/labels",
                headers=self._headers,
                json={"labels": labels},
            )
            r.raise_for_status()

    async def remove_label(self, number: int, label: str) -> None:
        encoded = _url_quote(label, safe="")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(
                f"{self._base}/issues/{number}/labels/{encoded}",
                headers=self._headers,
            )
            if r.status_code == 404:
                return
            r.raise_for_status()

    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base}/issues",
                headers=self._headers,
                json={"title": title, "body": body, "labels": labels},
            )
            r.raise_for_status()
            return r.json()

    async def add_sub_issue(self, parent_number: int, child_node_id: str) -> bool:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base}/issues/{parent_number}/sub_issues",
                headers=self._headers,
                json={"sub_issue_id": child_node_id},
            )
            if r.status_code in (403, 404):
                return False
            r.raise_for_status()
            return True

    async def fetch_sub_issues(self, parent_number: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/issues/{parent_number}/sub_issues",
                headers=self._headers,
                params={"per_page": 100},
            )
            if r.status_code in (403, 404):
                return []
            r.raise_for_status()
            return r.json()

    async def fetch_issues_by_label(self, label: str) -> list[Issue]:
        async with httpx.AsyncClient(timeout=30) as client:
            items = await self._paginate(client, {"state": "open", "labels": label})
        return [
            self._normalize(item)
            for item in items
            if "pull_request" not in item
        ]

    async def fetch_pr_for_branch(self, branch_name: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/pulls",
                headers=self._headers,
                params={"head": f"{self._owner}:{branch_name}", "state": "open"},
            )
            r.raise_for_status()
            prs = r.json()
            return prs[0] if prs else None

    async def fetch_pr_diff(self, pr_number: int) -> str:
        diff_headers = {**self._headers, "Accept": "application/vnd.github.v3.diff"}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/pulls/{pr_number}",
                headers=diff_headers,
            )
            r.raise_for_status()
            return r.text

    async def fetch_pr_checks(self, pr_number: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/pulls/{pr_number}",
                headers=self._headers,
            )
            r.raise_for_status()
            sha = r.json()["head"]["sha"]

            runs: list[dict] = []
            page = 1
            while True:
                r2 = await client.get(
                    f"{self._base}/commits/{sha}/check-runs",
                    headers=self._headers,
                    params={"per_page": 100, "page": page},
                )
                r2.raise_for_status()
                data = r2.json()
                batch = data.get("check_runs", [])
                runs.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
        return runs

    async def merge_pr(self, pr_number: int) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.put(
                f"{self._base}/pulls/{pr_number}/merge",
                headers=self._headers,
                json={"merge_method": "squash"},
            )
            r.raise_for_status()
