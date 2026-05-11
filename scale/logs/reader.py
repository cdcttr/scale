from __future__ import annotations
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Generator


def _key_input(inp: dict) -> str:
    for field in ("command", "file_path", "description", "query", "pattern"):
        if field in inp:
            return str(inp[field])
    return str(next(iter(inp.values()))) if inp else ""


def _fmt_prefix(elapsed_s: float | None, total_tokens: int) -> str:
    time_str = f"{int(elapsed_s):>3}s" if elapsed_s is not None else " ---"
    tokens_k = f"{total_tokens / 1000:.1f}k"
    return f"  {time_str}  {tokens_k:>6} tokens  "


def _format_event(
    event: dict,
    turn: int,
    elapsed_s: float | None = None,
    total_s: float | None = None,
    total_tokens: int | None = None,
) -> list[str]:
    etype = event.get("type")

    if etype == "assistant":
        message = event.get("message", {})
        content = message.get("content", [])
        if total_tokens is None:
            usage = message.get("usage") or {}
            total_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        prefix = _fmt_prefix(elapsed_s, total_tokens)
        pad = " " * len(prefix)

        lines: list[str] = []
        first = True
        for item in content:
            if item.get("type") == "text":
                text = item["text"].strip()
                if text:
                    p = prefix if first else pad
                    lines.append(f"[turn {turn}]{p}TEXT  {text}")
                    first = False
            elif item.get("type") == "tool_use":
                name = item.get("name", "")
                detail = _key_input(item.get("input") or {})
                p = prefix if first else pad
                lines.append(f"[turn {turn}]{p}TOOL  {name} — {detail}")
                first = False
        return lines

    if etype == "result":
        subtype = event.get("subtype", "unknown")
        usage = event.get("usage") or {}
        num_turns = event.get("num_turns", turn)
        in_tokens = usage.get("input_tokens", 0)
        out_tokens = usage.get("output_tokens", 0)
        in_k = f"{in_tokens / 1000:.1f}k"
        out_k = f"{out_tokens / 1000:.1f}k"
        base = f"[result] {subtype} after {num_turns} turns, {in_k} tokens in, {out_k} tokens out"
        if total_s is not None:
            return [f"{base}, {int(total_s)}s total"]
        return [base]

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
        cumulative_tokens = 0
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
                usage = (event.get("message") or {}).get("usage") or {}
                cumulative_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                yield from _format_event(event, turn, total_tokens=cumulative_tokens)
            elif etype == "result":
                yield from _format_event(event, turn)

    async def tail(self, workspace_dir: Path | None = None) -> None:
        turn = 0
        seen_bytes = 0
        cumulative_tokens = 0
        session_start = time.monotonic()
        last_turn_time = session_start

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
                    usage = (event.get("message") or {}).get("usage") or {}
                    cumulative_tokens += usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    now = time.monotonic()
                    elapsed_s = now - last_turn_time
                    last_turn_time = now
                    for line in _format_event(event, turn, elapsed_s=elapsed_s, total_tokens=cumulative_tokens):
                        print(line)
                elif etype == "result":
                    total_s = time.monotonic() - session_start
                    for line in _format_event(event, turn, total_s=total_s):
                        print(line)
                    done = True

            if done:
                return

            await asyncio.sleep(0.5)
