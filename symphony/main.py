from __future__ import annotations
import argparse
import asyncio
import logging
import sys
from pathlib import Path


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


async def _run(workflow_path: Path, port: int | None) -> None:
    from symphony.config.loader import load_workflow
    from symphony.tracker.github import GitHubClient
    from symphony.orchestrator.core import Orchestrator

    config = load_workflow(workflow_path)
    tracker = GitHubClient(config.tracker)
    orch = Orchestrator(config, tracker)

    tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    effective_port = port or (config.server.port if config.server else None)
    if effective_port:
        from symphony.api.server import create_app
        import uvicorn
        app = create_app(orch)
        server_config = uvicorn.Config(
            app, host="127.0.0.1", port=effective_port, log_level="warning"
        )
        server = uvicorn.Server(server_config)
        tasks.append(asyncio.create_task(server.serve()))

    if sys.stdout.isatty():
        from symphony.dashboard.ui import Dashboard
        dashboard = Dashboard(orch)
        tasks.append(asyncio.create_task(dashboard.run()))

    from symphony.config.watcher import watch_workflow

    def _on_reload(new_config) -> None:
        orch._config = new_config

    tasks.append(asyncio.create_task(watch_workflow(workflow_path, _on_reload)))
    tasks.append(asyncio.create_task(orch.run()))

    await asyncio.gather(*tasks)


async def _triage(
    workflow_path: Path,
    issue_numbers: list[int] | None,
    force_all: bool,
    model: str | None,
    dry_run: bool,
) -> None:
    from symphony.config.loader import load_workflow
    from symphony.config.schema import TriageConfig
    from symphony.tracker.github import GitHubClient
    from symphony.triage.runner import TriageRunner

    config = load_workflow(workflow_path)
    triage_config = config.triage or TriageConfig()
    if model:
        triage_config = triage_config.model_copy(update={"model": model})

    tracker = GitHubClient(config.tracker)
    runner = TriageRunner(triage_config, config.codex, tracker, dry_run=dry_run)

    if issue_numbers:
        issues = await tracker.fetch_issues_by_numbers(issue_numbers)
    else:
        issues = await tracker.fetch_candidate_issues()

    await runner.run(issues, force=force_all)


async def _plan(
    workflow_path: Path,
    issue_numbers: list[int],
    dry_run: bool,
    force: bool,
) -> None:
    from symphony.config.loader import load_workflow
    from symphony.tracker.github import GitHubClient
    from symphony.planner.runner import PlannerRunner

    config = load_workflow(workflow_path)
    if not config.planner:
        print("Error: planner is not configured in WORKFLOW.md. Add a [planner] section.", file=sys.stderr)
        sys.exit(1)

    tracker = GitHubClient(config.tracker)
    runner = PlannerRunner(config.planner, config.codex, tracker, dry_run=dry_run)
    issues = await tracker.fetch_issues_by_numbers(issue_numbers)
    await runner.run(issues, force=force)


def main() -> None:
    from importlib.metadata import version as _pkg_version
    parser = argparse.ArgumentParser(description="Symphony — Claude Code orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Start the Symphony daemon")
    run_p.add_argument(
        "workflow",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    run_p.add_argument("--port", type=int, default=None, help="HTTP API port")
    run_p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    sub.add_parser("version", help="Print version and exit")

    triage_p = sub.add_parser("triage", help="Assess issue readiness and apply labels")
    triage_p.add_argument(
        "workflow",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    triage_p.add_argument(
        "--issue", "-i",
        dest="issues",
        default=None,
        metavar="N[,N,...]",
        help="Comma-separated issue numbers to triage",
    )
    triage_p.add_argument(
        "--all",
        action="store_true",
        dest="force_all",
        help="Force re-triage all issues, even if already current",
    )
    triage_p.add_argument(
        "--model",
        default=None,
        help="Override the LLM model for this run",
    )
    triage_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print assessment to stdout, do not post to GitHub or apply labels",
    )
    triage_p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    plan_p = sub.add_parser("plan", help="Decompose a high-level issue into child tasks")
    plan_p.add_argument(
        "workflow",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    plan_p.add_argument(
        "--issue", "-i",
        dest="issues",
        required=True,
        metavar="N[,N,...]",
        help="Comma-separated issue numbers to plan",
    )
    plan_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print decomposition to stdout, do not create issues or apply labels",
    )
    plan_p.add_argument(
        "--force",
        action="store_true",
        help="Re-decompose even if already symphony:planned",
    )
    plan_p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args()

    if args.command == "version":
        try:
            ver = _pkg_version("symphony")
        except Exception:
            ver = "0.1.0"
        print(f"symphony {ver}")
        return

    if args.command == "triage":
        _setup_logging(args.log_level)
        issue_numbers = (
            [int(n.strip()) for n in args.issues.split(",")]
            if args.issues
            else None
        )
        asyncio.run(_triage(Path(args.workflow), issue_numbers, args.force_all, args.model, args.dry_run))
        return

    if args.command == "plan":
        _setup_logging(args.log_level)
        issue_numbers = [int(n.strip()) for n in args.issues.split(",")]
        asyncio.run(_plan(Path(args.workflow), issue_numbers, args.dry_run, args.force))
        return

    _setup_logging(args.log_level)
    asyncio.run(_run(Path(args.workflow), args.port))


if __name__ == "__main__":
    main()
