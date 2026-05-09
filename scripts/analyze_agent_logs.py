#!/usr/bin/env python3
"""Analyze Scale agent stats from stats.jsonl."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def _load_records(stats_path: Path) -> list[dict]:
    if not stats_path.exists():
        print(f"No stats file found at {stats_path}", file=sys.stderr)
        return []
    records = []
    with stats_path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_duration(s: int | float) -> str:
    s = int(s)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _date_of(record: dict) -> str:
    ts = record.get("timestamp", "")
    return ts[:10] if ts else "unknown"


def show_trends(records: list[dict]) -> None:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_date[_date_of(r)].append(r)

    print(f"\n{'Date':<12} {'Runs':>5} {'Succ':>5} {'AvgTurns':>9} {'AvgIn':>8} {'AvgOut':>8} {'AvgDur':>10}")
    print("-" * 65)
    for date in sorted(by_date):
        group = by_date[date]
        successes = sum(1 for r in group if r.get("success"))
        avg_turns = sum(r.get("turns", 0) for r in group) / len(group)
        avg_in = sum(r.get("input_tokens", 0) for r in group) / len(group)
        avg_out = sum(r.get("output_tokens", 0) for r in group) / len(group)
        avg_dur = sum(r.get("duration_s", 0) for r in group) / len(group)
        print(
            f"{date:<12} {len(group):>5} {successes:>5} {avg_turns:>9.1f}"
            f" {_fmt_tokens(int(avg_in)):>8} {_fmt_tokens(int(avg_out)):>8}"
            f" {_fmt_duration(avg_dur):>10}"
        )


def show_recent(records: list[dict], n: int = 20) -> None:
    recent = records[-n:]
    print(f"\nLast {len(recent)} runs:\n")
    header = f"{'#':>5}  {'Title':<35} {'OK':>3} {'Turns':>5} {'In':>7} {'Out':>7} {'Dur':>8}"
    print(header)
    print("-" * len(header))
    for r in recent:
        title = (r.get("issue_title") or "")[:34]
        ok = "✓" if r.get("success") else "✗"
        print(
            f"{r.get('issue', '?'):>5}  {title:<35} {ok:>3}"
            f" {r.get('turns', 0):>5}"
            f" {_fmt_tokens(r.get('input_tokens', 0)):>7}"
            f" {_fmt_tokens(r.get('output_tokens', 0)):>7}"
            f" {_fmt_duration(r.get('duration_s', 0)):>8}"
        )


def main() -> None:
    stats_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("stats.jsonl")
    records = _load_records(stats_path)
    if not records:
        print("No records to display.")
        return

    print(f"\nLoaded {len(records)} run(s) from {stats_path}")
    show_trends(records)
    show_recent(records)


if __name__ == "__main__":
    main()
