from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import logging
from pathlib import Path

from scale.agent.claude import ClaudeRunner
from scale.config.schema import CodexConfig
from scale.init.prompt import SYSTEM_PROMPT

log = logging.getLogger(__name__)

_PACKAGE_MANAGERS: list[tuple[str, str]] = [
    ("pyproject.toml", "uv sync"),
    ("requirements.txt", "pip install -r requirements.txt"),
    ("package.json", "npm install"),
    ("Cargo.toml", "cargo build"),
    ("go.mod", "go mod download"),
    ("Makefile", "make"),
]

_WORKFLOW_EXAMPLE = Path(__file__).parent.parent.parent / "WORKFLOW.md.example"
_REVIEW_EXAMPLE = Path(__file__).parent.parent.parent / "REVIEW.md.example"


def _extract_code_blocks(text: str, label: str) -> list[str]:
    pattern = rf"```{label}\s*([\s\S]*?)```"
    return [m.group(1).strip() for m in re.finditer(pattern, text)]


def _detect_package_manager(cwd: Path) -> tuple[str | None, str | None]:
    for config_file, install_cmd in _PACKAGE_MANAGERS:
        if (cwd / config_file).exists():
            return config_file, install_cmd
    return None, None


def _parse_github_repo(url: str) -> str:
    m = re.match(r"git@github\.com:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    m = re.match(r"https?://(?:[^@]+@)?github\.com/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    print(f"Error: could not parse GitHub repo from remote URL: {url}", file=sys.stderr)
    sys.exit(1)


class InitRunner:
    def __init__(self, codex: CodexConfig) -> None:
        self._runner = ClaudeRunner(codex)

    def _check_overwrite(self, cwd: Path, force: bool) -> None:
        if (cwd / "WORKFLOW.md").exists() and not force:
            print(
                "Error: WORKFLOW.md already exists. Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)

    def gather_context(self, cwd: Path) -> dict:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=True,
            )
            repo = _parse_github_repo(result.stdout.strip())
        except subprocess.CalledProcessError:
            print(
                "Error: not in a git repository or no remote named 'origin'.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                check=True,
            )
            branch = result.stdout.strip().split("/")[-1]
        except subprocess.CalledProcessError:
            branch = "main"

        config_file, install_cmd = _detect_package_manager(cwd)

        config_content = ""
        if config_file and (cwd / config_file).exists():
            config_content = (cwd / config_file).read_text()[:4000]

        readme = ""
        readme_path = cwd / "README.md"
        if readme_path.exists():
            lines = readme_path.read_text().splitlines()
            readme = "\n".join(lines[:200])

        return {
            "repo": repo,
            "branch": branch,
            "install_cmd": install_cmd or "",
            "config_file": config_file,
            "config_content": config_content,
            "readme": readme,
        }

    def _build_prompt(self, context: dict, with_review: bool) -> str:
        workflow_example = _WORKFLOW_EXAMPLE.read_text() if _WORKFLOW_EXAMPLE.exists() else ""
        review_example = _REVIEW_EXAMPLE.read_text() if _REVIEW_EXAMPLE.exists() else ""

        parts = [
            SYSTEM_PROMPT,
            "",
            "## Project Context",
            f"- GitHub repo: {context['repo']}",
            f"- Default branch: {context['branch']}",
            f"- Package manager config: {context['config_file'] or 'none detected'}",
            f"- Install command: {context['install_cmd'] or 'unknown'}",
            "",
        ]

        if context.get("config_content"):
            parts += [
                f"## {context['config_file']}",
                context["config_content"],
                "",
            ]

        if context.get("readme"):
            parts += [
                "## README.md (first 200 lines)",
                context["readme"],
                "",
            ]

        parts += [
            "## WORKFLOW.md.example",
            workflow_example,
            "",
        ]

        if with_review:
            parts += [
                "## REVIEW.md.example",
                review_example,
                "",
            ]

        parts += [
            "## Instructions",
            f"Generate a complete WORKFLOW.md for the `{context['repo']}` project.",
            f"- tracker.repo: {context['repo']}",
            f"- hooks.after_create: git clone, then run `{context['install_cmd'] or '<install command>'}`",
            f"- hooks.before_run: fetch origin and reset to `{context['branch']}`",
            "- Keep the prompt template body short and generic",
            "- Use scale: labels (not symphony: labels)",
            "- Leave optional sections (triage, planner, review) as commented-out blocks",
            "",
            "Output WORKFLOW.md as a fenced code block labeled yaml (```yaml ... ```).",
        ]

        if with_review:
            parts.append(
                "Also output REVIEW.md as a fenced code block labeled markdown (```markdown ... ```)."
            )

        return "\n".join(parts)

    async def run(
        self,
        cwd: Path,
        with_review: bool = False,
        dry_run: bool = False,
        force: bool = False,
    ) -> None:
        self._check_overwrite(cwd, force)

        context = self.gather_context(cwd)
        prompt = self._build_prompt(context, with_review)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await self._runner.run_turn(
                workspace=Path(tmpdir),
                prompt=prompt,
                is_continuation=False,
                model="claude-sonnet-4-6",
            )

        if not result.success:
            print(
                f"Error: Claude failed to generate WORKFLOW.md: {result.message}",
                file=sys.stderr,
            )
            sys.exit(1)

        yaml_blocks = _extract_code_blocks(result.message, "yaml")
        if not yaml_blocks:
            print(
                "Error: Claude did not produce a yaml code block for WORKFLOW.md.",
                file=sys.stderr,
            )
            sys.exit(1)

        workflow_content = yaml_blocks[0]

        review_content: str | None = None
        if with_review:
            md_blocks = _extract_code_blocks(result.message, "markdown")
            if md_blocks:
                review_content = md_blocks[0]

        if dry_run:
            print("=== WORKFLOW.md ===")
            print(workflow_content)
            if review_content:
                print("\n=== REVIEW.md ===")
                print(review_content)
            return

        (cwd / "WORKFLOW.md").write_text(workflow_content)
        log.info("Wrote WORKFLOW.md")
        print("Wrote WORKFLOW.md")

        if review_content:
            (cwd / "REVIEW.md").write_text(review_content)
            log.info("Wrote REVIEW.md")
            print("Wrote REVIEW.md")

        print("\nNext steps:")
        print("  export GITHUB_TOKEN=$(gh auth token)")
        print("  scale run WORKFLOW.md")
