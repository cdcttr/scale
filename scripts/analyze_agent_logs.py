#!/usr/bin/env python3
"""Analyze Scale agent stats from stats.jsonl."""
from __future__ import annotations

import argparse
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


def _is_real(record: dict) -> bool:
    """Filter out test/synthetic records (zero duration, placeholder titles, issue #1/#2)."""
    if record.get("duration_s", 0) == 0 and record.get("turns", 0) <= 1:
        return False
    title = (record.get("issue_title") or "").strip()
    if title in ("T", "Fix bug", "") and record.get("issue", 0) <= 2:
        return False
    return True


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_duration(s: int | float) -> str:
    s = int(s)
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _date_of(record: dict) -> str:
    ts = record.get("timestamp", "")
    return ts[:10] if ts else "unknown"


def show_trends(records: list[dict]) -> None:
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_date[_date_of(r)].append(r)

    print(f"\n{'Date':<12} {'Runs':>5} {'Succ%':>6} {'AvgTurns':>9} {'AvgIn':>8} {'AvgOut':>8} {'AvgDur':>10} {'MaxTurns':>9}")
    print("-" * 77)
    for date in sorted(by_date):
        group = by_date[date]
        successes = sum(1 for r in group if r.get("success"))
        avg_turns = sum(r.get("turns", 0) for r in group) / len(group)
        max_turns = max(r.get("turns", 0) for r in group)
        avg_in = sum(r.get("input_tokens", 0) for r in group) / len(group)
        avg_out = sum(r.get("output_tokens", 0) for r in group) / len(group)
        avg_dur = sum(r.get("duration_s", 0) for r in group) / len(group)
        pct = f"{100 * successes // len(group)}%"
        print(
            f"{date:<12} {len(group):>5} {pct:>6} {avg_turns:>9.1f}"
            f" {_fmt_tokens(int(avg_in)):>8} {_fmt_tokens(int(avg_out)):>8}"
            f" {_fmt_duration(avg_dur):>10} {max_turns:>9}"
        )


def show_runs(records: list[dict], n: int | None = None, failures_only: bool = False) -> None:
    filtered = [r for r in records if not failures_only or not r.get("success")]
    shown = filtered if n is None else filtered[-n:]
    label = "All" if n is None else f"Last {len(shown)}"
    qualifier = " (failures only)" if failures_only else ""
    print(f"\n{label} runs{qualifier}:\n")
    header = f"{'#':>5}  {'Title':<38} {'OK':>3} {'Turns':>5} {'In':>8} {'Out':>8} {'Dur':>9}"
    print(header)
    print("-" * len(header))
    for r in shown:
        title = (r.get("issue_title") or "")[:37]
        ok = "✓" if r.get("success") else "✗"
        print(
            f"{r.get('issue', '?'):>5}  {title:<38} {ok:>3}"
            f" {r.get('turns', 0):>5}"
            f" {_fmt_tokens(r.get('input_tokens', 0)):>8}"
            f" {_fmt_tokens(r.get('output_tokens', 0)):>8}"
            f" {_fmt_duration(r.get('duration_s', 0)):>9}"
        )


def show_outliers(records: list[dict], top_n: int = 5) -> None:
    if not records:
        return
    by_turns = sorted(records, key=lambda r: r.get("turns", 0), reverse=True)
    print(f"\nTop {top_n} by turn count:\n")
    header = f"{'#':>5}  {'Title':<38} {'OK':>3} {'Turns':>5} {'In':>8} {'Dur':>9}"
    print(header)
    print("-" * len(header))
    for r in by_turns[:top_n]:
        title = (r.get("issue_title") or "")[:37]
        ok = "✓" if r.get("success") else "✗"
        print(
            f"{r.get('issue', '?'):>5}  {title:<38} {ok:>3}"
            f" {r.get('turns', 0):>5}"
            f" {_fmt_tokens(r.get('input_tokens', 0)):>8}"
            f" {_fmt_duration(r.get('duration_s', 0)):>9}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Scale agent stats from stats.jsonl")
    parser.add_argument("stats", nargs="?", default="stats.jsonl", help="Path to stats.jsonl")
    parser.add_argument("-n", "--recent", type=int, default=20, help="Number of recent runs to show (0 = all)")
    parser.add_argument("--all", action="store_true", help="Show all runs (same as -n 0)")
    parser.add_argument("--failures", action="store_true", help="Show only failed runs")
    parser.add_argument("--outliers", type=int, default=5, metavar="N", help="Show top N by turn count")
    parser.add_argument("--include-synthetic", action="store_true", help="Include test/synthetic records")
    parser.add_argument("--date", help="Filter to a specific date (YYYY-MM-DD)")
    args = parser.parse_args()

    stats_path = Path(args.stats)
    all_records = _load_records(stats_path)
    if not all_records:
        print("No records to display.")
        return

    records = all_records if args.include_synthetic else [r for r in all_records if _is_real(r)]
    if args.date:
        records = [r for r in records if _date_of(r) == args.date]

    filtered_count = len(all_records) - len(records)
    print(f"\nLoaded {len(all_records)} run(s) from {stats_path}", end="")
    if filtered_count:
        print(f" ({filtered_count} synthetic filtered out, {len(records)} real)", end="")
    if args.date:
        print(f" — filtered to {args.date}", end="")
    print()

    if not records:
        print("No matching records.")
        return

    show_trends(records)

    n = None if (args.all or args.recent == 0) else args.recent
    show_runs(records, n=n, failures_only=args.failures)
    show_outliers(records, top_n=args.outliers)


if __name__ == "__main__":
    main()
