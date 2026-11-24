"""BeadsTracker: Scale's TrackerClient over a beads (bd) queue.

Work comes from `bd ready` (open, no active blockers, dependency-aware), filtered
by tracker.active_labels (e.g. "agent-ok") so agents never pick up human tasks.

Mapping onto Scale's GitHub-shaped model:
  - Issue.id / identifier  = bead id (e.g. "sudoku-s6y")
  - Issue.number           = a stable positive int derived from the bead id
                             (Scale keys state by int); resolved back on demand
  - branch                 = scale/<bead-id>
  - state                  = terminal if closed or carrying a terminal label;
                             ignored if in_progress/blocked/deferred (someone is on it),
                             skip-labelled, or missing an active label; else active
  - comments               = bd comments (text -> body)
  - adding a terminal label also closes the bead, so the queue stays truthful.

Run against tracker.beads_path (the project directory holding .beads). Actions
are recorded with actor "scale" in the bead history.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scale.config.schema import TrackerConfig
from scale.tracker.base import TrackerClient
from scale.tracker.models import Issue

logger = logging.getLogger(__name__)

_IGNORED_STATUSES = {"in_progress", "blocked", "deferred", "hooked", "pinned"}


def bead_number(bead_id: str) -> int:
    """Stable positive 31-bit int for a bead id."""
    return int.from_bytes(hashlib.sha256(bead_id.encode()).digest()[:4], "big") & 0x7FFFFFFF or 1


def _ts(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class BeadsTracker(TrackerClient):
    def __init__(self, config: TrackerConfig) -> None:
        if not config.beads_path:
            raise ValueError("tracker.kind=beads needs tracker.beads_path (the project directory with .beads)")
        self._config = config
        self._cwd = Path(config.beads_path).expanduser()
        self._ids: dict[int, str] = {}

    async def _bd(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "bd", *args, cwd=str(self._cwd),
            env={**os.environ, "BD_NON_INTERACTIVE": "1", "BEADS_ACTOR": os.environ.get("BEADS_ACTOR", "scale")},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"bd {' '.join(args[:2])} failed (exit {proc.returncode}): {err.decode(errors='replace').strip()[:300]}")
        return out.decode(errors="replace")

    async def _bd_json(self, *args: str):
        out = await self._bd(*args, "--json")
        data = json.loads(out) if out.strip() else []
        return data

    def _state(self, bead: dict) -> str:
        labels = set(bead.get("labels") or [])
        status = bead.get("status", "open")
        if status == "closed" or labels & set(self._config.terminal_labels):
            return "terminal"
        if status in _IGNORED_STATUSES:
            return "ignored"
        if labels & set(self._config.skip_labels):
            return "ignored"
        if self._config.active_labels and not all(l in labels for l in self._config.active_labels):
            return "ignored"
        return "active"

    def _normalize(self, bead: dict) -> Issue:
        bid = bead["id"]
        number = bead_number(bid)
        self._ids[number] = bid
        return Issue(
            id=bid,
            identifier=bid,
            number=number,
            title=bead.get("title", ""),
            description=bead.get("description") or "",
            state=self._state(bead),
            labels=list(bead.get("labels") or []),
            branch_name=f"scale/{bid}",
            url=f"beads://{self._cwd.name}/{bid}",
            priority=bead.get("priority"),
            created_at=_ts(bead.get("created_at")),
            updated_at=_ts(bead.get("updated_at")),
        )

    async def _resolve(self, number: int) -> str:
        if number not in self._ids:
            for bead in await self._bd_json("list", "--all", "-n", "0"):
                self._ids[bead_number(bead["id"])] = bead["id"]
        if number not in self._ids:
            raise KeyError(f"no bead maps to number {number}")
        return self._ids[number]

    async def fetch_candidate_issues(self) -> list[Issue]:
        args = ["ready", "-n", "0"]
        for label in self._config.active_labels:
            args += ["-l", label]
        issues = [self._normalize(b) for b in await self._bd_json(*args)]
        return [i for i in issues if i.state == "active"]

    async def fetch_open_issues(self) -> list[Issue]:
        return [self._normalize(b) for b in await self._bd_json("list", "-n", "0")]

    async def fetch_issues_by_numbers(self, numbers: list[int]) -> list[Issue]:
        ids = []
        for n in numbers:
            try:
                ids.append(await self._resolve(n))
            except KeyError:
                logger.warning("beads: no bead for number %d", n)
        if not ids:
            return []
        data = await self._bd_json("show", *ids)
        beads = data if isinstance(data, list) else [data]
        return [self._normalize(b) for b in beads]

    async def fetch_terminal_issues(self) -> list[Issue]:
        issues = [self._normalize(b) for b in await self._bd_json("list", "--all", "-n", "0")]
        return [i for i in issues if i.state == "terminal"]

    async def fetch_issues_by_label(self, label: str) -> list[Issue]:
        return [self._normalize(b) for b in await self._bd_json("list", "-l", label, "-n", "0")]

    async def add_labels(self, number: int, labels: list[str]) -> None:
        bid = await self._resolve(number)
        for label in labels:
            await self._bd("label", "add", bid, label)
        if set(labels) & set(self._config.terminal_labels):
            await self._bd("close", bid, "--reason", "Completed by Scale")

    async def remove_label(self, number: int, label: str) -> None:
        await self._bd("label", "remove", await self._resolve(number), label)

    async def post_comment(self, number: int, body: str) -> None:
        await self._bd("comment", await self._resolve(number), body)

    async def fetch_issue_comments(self, number: int) -> list[dict]:
        data = await self._bd_json("comments", await self._resolve(number))
        return [
            {"body": c.get("text", ""), "created_at": c.get("created_at"),
             "user": {"login": c.get("author", "")}, "id": c.get("id")}
            for c in (data or [])
        ]

    async def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        args = ["create", title, "--description", body]
        if labels:
            args += ["-l", ",".join(labels)]
        bead = await self._bd_json(*args)
        bead = bead[0] if isinstance(bead, list) else bead
        number = bead_number(bead["id"])
        self._ids[number] = bead["id"]
        return {"number": number, "node_id": bead["id"], "id": bead["id"],
                "html_url": f"beads://{self._cwd.name}/{bead['id']}"}

    async def add_sub_issue(self, parent_number: int, child_node_id: str) -> bool:
        try:
            parent = await self._resolve(parent_number)
            await self._bd("dep", "add", child_node_id, parent, "--type", "parent-child")
            return True
        except Exception as e:
            logger.warning("beads: could not link %s under %d: %s", child_node_id, parent_number, e)
            return False
