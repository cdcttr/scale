from __future__ import annotations
import asyncio
import json
import re
from pathlib import Path
from typing import Generator


def _key_input(inp: dict) -> str:
    for field in ("command", "file_path", "description", "query", "pattern"):
        if field in inp:
            return str(inp[field])
    return str(next(iter(inp.values()))) if inp else ""


def _format_event(event: dict, turn: int) -> list[str]:
    etype = event.get("type")

    if etype == "assistant":
        message = event.get("message", {})
        content = message.get("content", [])
        lines = []
        for item in content:
            if item.get("type") == "text":
                text = item["text"].strip()
                if text:
                    lines.append(f"[turn {turn}] TEXT  {text}")
            elif item.get("type") == "tool_use":
                name = item.get("name", "")
                detail = _key_input(item.get("input") or {})
                lines.append(f"[turn {turn}] TOOL  {name} — {detail}")
        return lines

    if etype == "result":
        subtype = event.get("subtype", "unknown")
        usage = event.get("usage") or {}
        num_turns = event.get("num_turns", turn)
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        in_k = f"{in_tokens / 1000:.1f}k"
        out_k = f"{out_tokens / 1000:.1f}k"
        return [f"[result] {subtype} after {num_turns} turns, {in_k} tokens in, {out_k} tokens out"]

    return []


def find_workspace(workspace_root: Path, issue_number: int) -> Path | None:
    if not workspace_root.exists():
        return None
    for path in workspace_root.iterdir():
        if path.is_dir() and re.search(rf"_{issue_number}$", path.name):
            return path
    return None


def find_archived_log(log_archive: Path, issue_number: int) -> Path | None:
    if not log_archive.exists():
        return None
    matches = sorted(log_archive.glob(f"{issue_number}-*.log"))
    return matches[-1] if matches else None


class LogReader:
    def __init__(self, log_path: Path) -> None:
        self._path = log_path

    def iter_formatted(self) -> Generator[str, None, None]:
        turn = 0
        for raw_line in self._path.read_text().splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "assistant":
                turn += 1
                yield from _format_event(event, turn)
            elif etype == "result":
                yield from _format_event(event, turn)

    async def tail(self, workspace_dir: Path | None = None) -> None:
        turn = 0
        seen_bytes = 0

        while True:
            if workspace_dir is not None and not workspace_dir.exists():
                return

            if not self._path.exists():
                await asyncio.sleep(0.5)
                continue

            content = self._path.read_bytes()
            new_content = content[seen_bytes:].decode(errors="replace")
            seen_bytes = len(content)

            done = False
            for raw_line in new_content.splitlines():
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "assistant":
                    turn += 1
                    for line in _format_event(event, turn):
                        print(line)
                elif etype == "result":
                    for line in _format_event(event, turn):
                        print(line)
                    done = True

            if done:
                return

            await asyncio.sleep(0.5)
