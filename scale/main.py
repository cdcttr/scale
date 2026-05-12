from __future__ import annotations
import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console


def _setup_logging(level: str, console: Console | None = None) -> None:
    if console is not None:
        from rich.logging import RichHandler
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(message)s",
            handlers=[RichHandler(console=console, show_path=False)],
        )
    else:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stderr,
        )
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _run(workflow_path: Path, port: int | None, console: Console | None = None) -> None:
    from scale.config.loader import load_workflow
    from scale.tracker.github import GitHubClient
    from scale.orchestrator.core import Orchestrator

    config = load_workflow(workflow_path)
    tracker = GitHubClient(config.tracker)
    orch = Orchestrator(config, tracker, scm=tracker)

    tasks: list[asyncio.Task] = []  # type: ignore[type-arg]

    effective_port = port or (config.server.port if config.server else None)
    if effective_port:
        from scale.api.server import create_app
        import uvicorn
        app = create_app(orch, api_token=config.server.api_token if config.server else None)
        server_config = uvicorn.Config(
            app, host="127.0.0.1", port=effective_port, log_level="warning"
        )
        server = uvicorn.Server(server_config)
        tasks.append(asyncio.create_task(server.serve()))

    if sys.stdout.isatty():
        from scale.dashboard.ui import Dashboard
        dashboard = Dashboard(orch, console=console)
        tasks.append(asyncio.create_task(dashboard.run()))

    from scale.config.watcher import watch_workflow

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
    from scale.config.loader import load_workflow
    from scale.config.schema import TriageConfig
    from scale.tracker.github import GitHubClient
    from scale.triage.runner import TriageRunner

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


async def _clean(
    workflow_path: Path,
    dry_run: bool,
    all_workspaces: bool,
    yes: bool,
) -> None:
    from scale.config.loader import load_workflow
    from scale.clean import clean

    config = load_workflow(workflow_path)
    await clean(config, dry_run=dry_run, all_workspaces=all_workspaces, yes=yes)


async def _init(
    cwd: Path,
    with_review: bool,
    dry_run: bool,
    force: bool,
) -> None:
    from scale.config.schema import CodexConfig
    from scale.init.runner import InitRunner

    runner = InitRunner(CodexConfig())
    await runner.run(cwd, with_review=with_review, dry_run=dry_run, force=force)


async def _plan(
    workflow_path: Path,
    issue_numbers: list[int],
    dry_run: bool,
    force: bool,
) -> None:
    from scale.config.loader import load_workflow
    from scale.tracker.github import GitHubClient
    from scale.planner.runner import PlannerRunner

    config = load_workflow(workflow_path)
    assert config.planner is not None, "planner not configured"
    tracker = GitHubClient(config.tracker)
    runner = PlannerRunner(config.planner, config.codex, tracker, dry_run=dry_run)
    issues = await tracker.fetch_issues_by_numbers(issue_numbers)
    await runner.run(issues, force=force)


async def _logs(
    workflow_path: Path,
    issue_number: int,
    show_all: bool,
    archived: bool,
) -> None:
    from scale.config.loader import load_workflow
    from scale.logs.reader import LogReader, find_archived_log, find_workspace

    config = load_workflow(workflow_path)
    workspace_root = Path(config.workspace.root)

    if archived:
        if not config.workspace.log_archive:
            print("Error: log_archive is not configured in WORKFLOW.md", file=sys.stderr)
            sys.exit(1)
        log_path = find_archived_log(Path(config.workspace.log_archive), issue_number)
        if log_path is None:
            print(f"Error: no archived log found for issue #{issue_number}", file=sys.stderr)
            sys.exit(1)
        for line in LogReader(log_path).iter_formatted():
            print(line)
        return

    workspace_dir = find_workspace(workspace_root, issue_number)
    if workspace_dir is None:
        print(f"Error: no workspace found for issue #{issue_number}", file=sys.stderr)
        sys.exit(1)

    log_path = workspace_dir / "agent.log"
    if show_all:
        if not log_path.exists():
            print(f"Error: agent.log not found in {workspace_dir}", file=sys.stderr)
            sys.exit(1)
        for line in LogReader(log_path).iter_formatted():
            print(line)
        return

    await LogReader(log_path).tail(workspace_dir=workspace_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scale — Claude Code orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Start the Scale daemon")
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

    init_p = sub.add_parser("init", help="Generate WORKFLOW.md for the current project")
    init_p.add_argument(
        "--with-review",
        action="store_true",
        help="Also generate REVIEW.md",
    )
    init_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print generated content to stdout, do not write files",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing WORKFLOW.md",
    )
    init_p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

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

    clean_p = sub.add_parser("clean", help="Remove stale workspace directories")
    clean_p.add_argument(
        "workflow",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    clean_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed without deleting",
    )
    clean_p.add_argument(
        "--all",
        action="store_true",
        dest="all_workspaces",
        help="Remove all workspace directories regardless of issue state",
    )
    clean_p.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt when used with --all",
    )
    clean_p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    logs_p = sub.add_parser("logs", help="Stream human-readable agent activity for an issue")
    logs_p.add_argument("issue", type=int, help="Issue number")
    logs_p.add_argument(
        "workflow",
        nargs="?",
        default="WORKFLOW.md",
        help="Path to WORKFLOW.md (default: ./WORKFLOW.md)",
    )
    logs_p.add_argument(
        "--all",
        action="store_true",
        dest="all",
        help="Show full log from the beginning instead of tailing",
    )
    logs_p.add_argument(
        "--archived",
        action="store_true",
        help="Read from log_archive instead of active workspace",
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command not in ("version", "init"):
        workflow_path = Path(args.workflow)
        if not workflow_path.exists():
            if args.workflow == "WORKFLOW.md":
                print(
                    "Error: no WORKFLOW.md found in current directory. "
                    "Pass a path explicitly or create WORKFLOW.md here.",
                    file=sys.stderr,
                )
            else:
                print(f"Error: workflow file not found: {args.workflow}", file=sys.stderr)
            sys.exit(1)

    if args.command == "init":
        _setup_logging(args.log_level)
        asyncio.run(_init(Path("."), args.with_review, args.dry_run, args.force))
        return

    if args.command == "version":
        from importlib.metadata import version as _pkg_version
        try:
            ver = _pkg_version("scale")
        except Exception:
            ver = "0.1.0"
        print(f"scale {ver}")
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
        from scale.config.loader import load_workflow
        config = load_workflow(Path(args.workflow))
        if not config.planner:
            print("Error: planner is not configured in WORKFLOW.md. Add a [planner] section.", file=sys.stderr)
            sys.exit(1)
        issue_numbers = [int(n.strip()) for n in args.issues.split(",")]
        asyncio.run(_plan(Path(args.workflow), issue_numbers, args.dry_run, args.force))
        return

    if args.command == "clean":
        _setup_logging(args.log_level)
        asyncio.run(_clean(Path(args.workflow), args.dry_run, args.all_workspaces, args.yes))
        return

    if args.command == "logs":
        asyncio.run(_logs(Path(args.workflow), args.issue, args.all, args.archived))
        return

    console = Console()
    _setup_logging(args.log_level, console=console)
    asyncio.run(_run(Path(args.workflow), args.port, console=console))


if __name__ == "__main__":
    main()
