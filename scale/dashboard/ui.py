from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich import box

if TYPE_CHECKING:
    from scale.orchestrator.core import Orchestrator


def _elapsed(dt: datetime) -> str:
    delta = datetime.now(tz=timezone.utc) - dt.replace(tzinfo=timezone.utc)
    s = int(delta.total_seconds())
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s"


def _ago(dt: datetime) -> str:
    delta = datetime.now(tz=timezone.utc) - dt.replace(tzinfo=timezone.utc)
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    m = s // 60
    if m < 60:
        return f"{m}m ago"
    return f"{m // 60}h ago"


def _fmt_tokens(n: int) -> str:
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def _build_table(orch: "Orchestrator") -> Table:
    state = orch.get_state()
    now_str = datetime.now().strftime("%m/%d %H:%M:%S")

    table = Table(box=box.SIMPLE, show_header=False, expand=True)
    table.add_column("", style="bold cyan", width=6, no_wrap=True)
    table.add_column("", style="white", ratio=1)
    table.add_column("", justify="right", style="dim")
    table.add_column("", justify="right", style="green")
    table.add_column("", justify="right", style="yellow")
    table.add_column("", justify="right", style="dim")

    table.add_row(
        "",
        Text(
            f"Scale  ●  {len(state.running)} running  "
            f"{len(state.retry_queue)} retrying  "
            f"{len(state.completed)} completed",
            style="bold",
        ),
        "", "", "", now_str,
    )
    table.add_row("", "", "", "", "", "")

    if state.running:
        table.add_row("", Text("RUNNING", style="bold underline"), "", "", "", "")
        for session in state.running.values():
            d = "[dim]" if session.finishing else ""
            e = "[/dim]" if session.finishing else ""
            table.add_row(
                f"{d}#{session.issue.number}{e}",
                f"{d}{session.issue.title[:60]}{e}",
                f"{d}turn {session.turn_count}{e}",
                f"{d}{_fmt_tokens(session.tokens.input_tokens)} in{e}",
                f"{d}{_fmt_tokens(session.tokens.output_tokens)} out{e}",
                f"{d}{_elapsed(session.started_at)}{e}",
            )

    if state.secondary:
        table.add_row("", "", "", "", "", "")
        table.add_row("", Text("REVIEWING", style="bold underline"), "", "", "", "")
        for sess in state.secondary.values():
            table.add_row(
                f"#{sess.issue.number}",
                sess.issue.title[:60],
                sess.kind,
                "", "",
                _elapsed(sess.started_at),
            )

    if state.retry_queue:
        table.add_row("", "", "", "", "", "")
        table.add_row("", Text("RETRYING", style="bold underline"), "", "", "", "")
        for entry in state.retry_queue:
            delta = (
                entry.due_at.replace(tzinfo=timezone.utc)
                - datetime.now(tz=timezone.utc)
            ).total_seconds()
            retry_in = f"retry in {max(0, int(delta))}s"
            table.add_row(
                f"#{entry.issue.number}",
                entry.issue.title[:60],
                f"attempt {entry.attempt}",
                "", retry_in,
                entry.error[:30] if entry.error else "",
            )

    if state.completed:
        table.add_row("", "", "", "", "", "")
        table.add_row("", Text("RECENTLY COMPLETED", style="bold underline"), "", "", "", "")
        for cs in reversed(state.completed):
            age_s = int(
                (datetime.now(tz=timezone.utc) - cs.completed_at.replace(tzinfo=timezone.utc))
                .total_seconds()
            )
            row_style = "dim" if age_s >= 60 else None
            turns_label = f"{cs.turn_count} turn{'s' if cs.turn_count != 1 else ''}"
            table.add_row(
                f"#{cs.issue.number}",
                cs.issue.title[:60],
                turns_label,
                f"{_fmt_tokens(cs.tokens.total)} tokens",
                "",
                f"completed {_ago(cs.completed_at)}",
                style=row_style,
            )

    table.add_row("", "", "", "", "", "")
    table.add_row(
        "",
        Text(
            f"TOTALS  {_fmt_tokens(state.token_totals.total)} tokens  •  "
            f"{state.total_completed} completed",
            style="dim",
        ),
        "", "", "", "",
    )
    return table


class Dashboard:
    def __init__(self, orch: "Orchestrator", console: Console | None = None) -> None:
        self._orch = orch
        self._console = console or Console()

    async def run(self) -> None:
        with Live(console=self._console, refresh_per_second=0.5) as live:
            while True:
                live.update(_build_table(self._orch))
                await asyncio.sleep(2)
