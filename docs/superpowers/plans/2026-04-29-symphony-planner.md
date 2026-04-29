# Symphony Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a planning layer that decomposes high-level GitHub Issues into leaf-level child tasks, and refactor all AI operations to use the `claude` CLI via `ClaudeRunner` instead of the Anthropic SDK directly.

**Architecture:** A new `symphony/planner/` module mirrors the structure of `symphony/triage/`. `ClaudeRunner` gains an optional `--model` flag. `TriageAgent` is refactored to use `ClaudeRunner` (removing the `anthropic` SDK dependency). The orchestrator grows two new capabilities: detecting `symphony:plan` labels in `_tick()` and a `_watch_planned()` task that closes parents when all children complete.

**Tech Stack:** Python asyncio, httpx, Pydantic v2, pytest-asyncio, respx (for HTTP mocking), existing `ClaudeRunner` subprocess pattern.

---

## Task 1: Add `--model` flag to `ClaudeRunner`

**Files:**
- Modify: `symphony/agent/claude.py`
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_agent.py`:

```python
def test_build_cmd_with_model():
    runner = ClaudeRunner(CodexConfig())
    cmd = runner._build_cmd("prompt", False, model="claude-haiku-4-5-20251001")
    assert "--model" in cmd
    idx = cmd.index("--model")
    assert cmd[idx + 1] == "claude-haiku-4-5-20251001"


def test_build_cmd_without_model_omits_flag():
    runner = ClaudeRunner(CodexConfig())
    cmd = runner._build_cmd("prompt", False, model=None)
    assert "--model" not in cmd


@pytest.mark.asyncio
async def test_run_turn_passes_model_to_cmd(tmp_path: Path):
    event = json.dumps({
        "type": "result", "subtype": "success", "result": "Done",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=_make_proc([event]))) as mock_exec:
        await _runner().run_turn(tmp_path, "prompt", False, model="claude-haiku-4-5-20251001")
    cmd_args = mock_exec.call_args[0]
    assert "--model" in cmd_args
    idx = list(cmd_args).index("--model")
    assert cmd_args[idx + 1] == "claude-haiku-4-5-20251001"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_agent.py::test_build_cmd_with_model tests/test_agent.py::test_build_cmd_without_model_omits_flag tests/test_agent.py::test_run_turn_passes_model_to_cmd -v
```

Expected: FAIL with `TypeError` (unexpected keyword argument `model`)

- [ ] **Step 3: Implement the model flag**

Replace `_build_cmd` and `run_turn` in `symphony/agent/claude.py`:

```python
def _build_cmd(self, prompt: str, is_continuation: bool, model: Optional[str] = None) -> list[str]:
    cmd = [
        self._config.command,
        "--print",
        "--output-format", "stream-json",
        "--dangerously-skip-permissions",
        "--max-turns", "1",
    ]
    if model:
        cmd += ["--model", model]
    if is_continuation:
        cmd.append("--continue")
    cmd += ["-p", prompt]
    return cmd

async def run_turn(
    self,
    workspace: Path,
    prompt: str,
    is_continuation: bool,
    on_event: Optional[Callable[[dict], None]] = None,
    model: Optional[str] = None,
) -> TurnResult:
    cmd = self._build_cmd(prompt, is_continuation, model)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    result: Optional[TurnResult] = None

    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        line = raw_line.decode().strip()
        if not line:
            continue
        parsed = parse_stream_event(line)
        if parsed is not None:
            result = parsed
        if on_event:
            try:
                event = json.loads(line)
                on_event(event)
            except json.JSONDecodeError:
                pass

    await proc.wait()

    if proc.returncode != 0 and result is None:
        return TurnResult(success=False, usage=None, message=f"Exit code {proc.returncode}")
    if result is None:
        return TurnResult(success=False, usage=None, message="No result event received")
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_agent.py -v
```

Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add symphony/agent/claude.py tests/test_agent.py
git commit -m "feat: add optional --model flag to ClaudeRunner"
```

---

## Task 2: Refactor `TriageAgent` to use `ClaudeRunner`

**Files:**
- Modify: `symphony/triage/agent.py`
- Modify: `symphony/triage/runner.py`
- Modify: `symphony/main.py`
- Modify: `tests/test_triage_agent.py`
- Modify: `tests/test_triage_runner.py`

The existing `TriageAgent` calls `Anthropic().messages.create()`. We replace it with `ClaudeRunner.run_turn()`. The `assess` method becomes `async`. `TriageRunner` passes `CodexConfig` through to `TriageAgent`.

- [ ] **Step 1: Rewrite `tests/test_triage_agent.py`**

Replace the entire file:

```python
import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from symphony.triage.agent import TriageAgent, TriageAssessment
from symphony.tracker.models import Issue
from symphony.config.schema import CodexConfig, TriageConfig
from symphony.agent.claude import TurnResult


def _config(**kwargs) -> TriageConfig:
    return TriageConfig(**kwargs)


def _codex() -> CodexConfig:
    return CodexConfig()


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Add dark mode",
        description="Add a dark mode toggle to the settings page.",
        state="active", labels=[], branch_name="symphony/1-add-dark-mode",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def _turn_result(text: str, success: bool = True) -> TurnResult:
    return TurnResult(success=success, usage=None, message=text)


@pytest.mark.asyncio
async def test_assess_returns_ready_assessment(tmp_path: Path):
    payload = json.dumps({
        "ready": True,
        "summary": "Clear and actionable.",
        "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**\n\nClear.",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.ready is True
    assert result.summary == "Clear and actionable."
    assert result.reasons == []


@pytest.mark.asyncio
async def test_assess_returns_not_ready_assessment(tmp_path: Path):
    payload = json.dumps({
        "ready": False,
        "summary": "Missing acceptance criteria.",
        "reasons": ["No acceptance criteria", "Vague scope"],
        "comment": "## Symphony Triage\n\n**Status: Needs more detail ❌**",
    })
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is not None
    assert result.ready is False
    assert "No acceptance criteria" in result.reasons


@pytest.mark.asyncio
async def test_assess_handles_runner_failure(tmp_path: Path):
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(side_effect=Exception("network error"))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_turn_not_success(tmp_path: Path):
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result("crashed", success=False))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_bad_json(tmp_path: Path):
    agent = TriageAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result("not json"))):
        result = await agent.assess(_issue(), [], tmp_path)
    assert result is None


def test_build_prompt_includes_title_and_body():
    agent = TriageAgent(_config(), _codex())
    issue = _issue(title="Fix login bug", description="Login fails on Safari.")
    prompt = agent._build_prompt(issue, [])
    assert "Fix login bug" in prompt
    assert "Login fails on Safari." in prompt


def test_build_prompt_truncates_comments_to_20():
    agent = TriageAgent(_config(), _codex())
    comments = [
        {"user": {"login": f"user{i}"}, "body": f"comment {i}"}
        for i in range(25)
    ]
    prompt = agent._build_prompt(_issue(), comments)
    assert "comment 24" in prompt
    assert "comment 4" not in prompt


@pytest.mark.asyncio
async def test_assess_passes_model_to_runner(tmp_path: Path):
    payload = json.dumps({
        "ready": True, "summary": "OK.", "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**",
    })
    agent = TriageAgent(_config(model="claude-sonnet-4-6"), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn_result(payload))) as mock_run:
        await agent.assess(_issue(), [], tmp_path)
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs.get("model") == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_triage_agent.py -v
```

Expected: FAIL (TriageAgent still uses Anthropic SDK, wrong signatures)

- [ ] **Step 3: Rewrite `symphony/triage/agent.py`**

Replace the entire file:

```python
from __future__ import annotations
import json
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from symphony.agent.claude import ClaudeRunner
from symphony.config.schema import CodexConfig, TriageConfig
from symphony.tracker.models import Issue

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a triage agent for an autonomous coding system called Symphony.
Your job is to assess whether a GitHub issue is ready to be implemented autonomously.

An issue is READY if ALL of the following are true:
- The task is clearly stated (not a question or open-ended discussion)
- The scope is bounded — there is a defined "done" state
- Sufficient context is present to begin implementation without asking for clarification
- It is a coding/engineering task (not a process, policy, or design discussion)

An issue is NOT READY if ANY of the following apply:
- The title or body is vague (e.g., "fix the bug", "make it faster")
- Critical information is missing (which component, what behaviour, what the expected state is)
- The issue is a question or a discussion thread
- Multiple unrelated tasks are bundled together
- It depends on an external decision that has not been made

Respond with a JSON object only — no prose outside the JSON:
{
  "ready": true,
  "summary": "One-sentence verdict",
  "reasons": ["Only populated if not ready — specific gaps"],
  "comment": "Full markdown comment body to post on GitHub"
}

The comment field must follow exactly one of these formats:

Ready:
## Symphony Triage

**Status: Ready ✅**

This issue is clear and actionable. <one sentence explanation>

Not ready:
## Symphony Triage

**Status: Needs more detail ❌**

This issue needs clarification before it can be worked on autonomously:

- <specific gap>
- <specific gap>

Please add more detail and Symphony will re-evaluate when the issue is updated.\
"""


@dataclass
class TriageAssessment:
    ready: bool
    summary: str
    reasons: list[str] = field(default_factory=list)
    comment: str = ""


class TriageAgent:
    def __init__(self, config: TriageConfig, codex: CodexConfig) -> None:
        self._config = config
        self._runner = ClaudeRunner(codex)

    def _build_prompt(self, issue: Issue, comments: list[dict]) -> str:
        parts = [
            f"# Issue #{issue.number}: {issue.title}",
            "",
            "## Body",
            issue.description or "(no description)",
            "",
        ]
        if issue.labels:
            parts += ["## Labels", ", ".join(issue.labels), ""]
        if comments:
            parts += ["## Comments (newest last, up to 20)"]
            for c in comments[-20:]:
                author = c.get("user", {}).get("login", "unknown")
                parts.append(f"**{author}:** {c['body']}")
                parts.append("")
        parts += [
            "## Instructions",
            _SYSTEM_PROMPT,
        ]
        return "\n".join(parts)

    async def assess(
        self,
        issue: Issue,
        comments: list[dict],
        workspace: Path,
    ) -> TriageAssessment | None:
        prompt = self._build_prompt(issue, comments)
        try:
            result = await self._runner.run_turn(
                workspace=workspace,
                prompt=prompt,
                is_continuation=False,
                model=self._config.model,
            )
        except Exception as exc:
            log.error("Triage API call failed for issue #%d: %s", issue.number, exc)
            return None
        if not result.success:
            log.error("Triage call failed for issue #%d: %s", issue.number, result.message)
            return None
        try:
            data = json.loads(result.message)
            return TriageAssessment(
                ready=bool(data["ready"]),
                summary=data["summary"],
                reasons=data.get("reasons", []),
                comment=data.get("comment", ""),
            )
        except Exception as exc:
            log.error(
                "Triage JSON parse failed for issue #%d: %s\nRaw response: %s",
                issue.number, exc, result.message,
            )
            return None
```

- [ ] **Step 4: Update `symphony/triage/runner.py`**

`TriageRunner.__init__` gains a `codex` parameter; `triage_issue` awaits `assess` and passes a temp workspace. Replace the entire file:

```python
from __future__ import annotations
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from symphony.config.schema import CodexConfig, TriageConfig
from symphony.tracker.github import GitHubClient
from symphony.tracker.models import Issue
from symphony.triage.agent import TriageAgent, TriageAssessment

log = logging.getLogger(__name__)

_MARKER_PREFIX = "<!-- symphony-triage "
_MARKER_SUFFIX = " -->"


def _parse_triage_timestamp(comment_body: str) -> datetime | None:
    if not comment_body.startswith(_MARKER_PREFIX):
        return None
    end = comment_body.find(_MARKER_SUFFIX, len(_MARKER_PREFIX))
    if end == -1:
        return None
    ts_str = comment_body[len(_MARKER_PREFIX):end]
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def _needs_triage(issue: Issue, comments: list[dict], force: bool) -> bool:
    if force:
        return True
    triage_comments = [c for c in comments if c["body"].startswith(_MARKER_PREFIX)]
    if not triage_comments:
        return True
    last_triage = max(
        triage_comments,
        key=lambda c: datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")),
    )
    ts = _parse_triage_timestamp(last_triage["body"])
    if ts is None or ts.tzinfo is None:
        return True
    issue_updated = issue.updated_at
    if issue_updated.tzinfo is None:
        issue_updated = issue_updated.replace(tzinfo=timezone.utc)
    return issue_updated > ts


def _build_comment_body(assessment: TriageAssessment) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return f"{_MARKER_PREFIX}{ts}{_MARKER_SUFFIX}\n{assessment.comment}"


class TriageRunner:
    def __init__(
        self,
        config: TriageConfig,
        codex: CodexConfig,
        client: GitHubClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._client = client
        self._agent = TriageAgent(config, codex)
        self._dry_run = dry_run

    async def triage_issue(self, issue: Issue, force: bool = False) -> None:
        log.info("Checking issue #%d: %s", issue.number, issue.title)
        comments = await self._client.fetch_issue_comments(issue.number)

        if not _needs_triage(issue, comments, force):
            log.info("Issue #%d is already current, skipping", issue.number)
            return

        with tempfile.TemporaryDirectory() as tmpdir:
            assessment = await self._agent.assess(issue, comments, Path(tmpdir))

        if assessment is None:
            log.warning("Skipping issue #%d due to assessment failure", issue.number)
            return

        status = "ready" if assessment.ready else "not ready"
        log.info("Issue #%d: %s — %s", issue.number, status, assessment.summary)

        if self._dry_run:
            print(f"\n--- Issue #{issue.number}: {issue.title} ---")
            print(f"Ready: {assessment.ready}")
            print(f"Summary: {assessment.summary}")
            if assessment.reasons:
                print(f"Reasons: {assessment.reasons}")
            print(f"Comment:\n{_build_comment_body(assessment)}")
            return

        body = _build_comment_body(assessment)
        await self._client.post_comment(issue.number, body)

        if assessment.ready:
            await self._client.add_labels(
                issue.number,
                [self._config.ready_label, self._config.triaged_label],
            )
            await self._client.remove_label(issue.number, self._config.needs_detail_label)
        else:
            await self._client.add_labels(
                issue.number,
                [self._config.needs_detail_label, self._config.triaged_label],
            )
            await self._client.remove_label(issue.number, self._config.ready_label)

    async def run(self, issues: list[Issue], force: bool = False) -> None:
        for issue in issues:
            await self.triage_issue(issue, force=force)
```

- [ ] **Step 5: Update `symphony/main.py` — pass `config.codex` to `TriageRunner`**

In the `_triage` function, change the `TriageRunner` construction:

```python
runner = TriageRunner(triage_config, config.codex, tracker, dry_run=dry_run)
```

(was: `TriageRunner(triage_config, tracker, dry_run=dry_run)`)

- [ ] **Step 6: Update `tests/test_triage_runner.py`**

The `TriageRunner` constructor now requires a `CodexConfig`. Update all `TriageRunner(...)` calls in the file to add `CodexConfig()` as the second argument:

```python
from symphony.config.schema import CodexConfig, TriageConfig

# Every TriageRunner(...) call changes from:
runner = TriageRunner(_config(), gh)
# to:
runner = TriageRunner(_config(), CodexConfig(), gh)

# And for dry_run variant:
runner = TriageRunner(_config(), CodexConfig(), gh, dry_run=True)
```

Also patch `assess` as async:

```python
# All patch.object calls on runner._agent.assess need AsyncMock:
with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
```

- [ ] **Step 7: Run all triage tests**

```bash
pytest tests/test_triage_agent.py tests/test_triage_runner.py -v
```

Expected: All PASS

- [ ] **Step 8: Run the full test suite to check for regressions**

```bash
pytest -v
```

Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add symphony/triage/agent.py symphony/triage/runner.py symphony/main.py \
        tests/test_triage_agent.py tests/test_triage_runner.py
git commit -m "refactor: triage agent uses ClaudeRunner instead of Anthropic SDK"
```

---

## Task 3: Remove `anthropic` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Remove the dependency**

```bash
uv remove anthropic
```

- [ ] **Step 2: Verify no remaining imports**

```bash
grep -r "from anthropic" symphony/ tests/
grep -r "import anthropic" symphony/ tests/
```

Expected: no output

- [ ] **Step 3: Run tests to confirm nothing broke**

```bash
pytest -v
```

Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: remove anthropic SDK dependency"
```

---

## Task 4: Add `PlannerConfig` to config schema

**Files:**
- Modify: `symphony/config/schema.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
from symphony.config.schema import PlannerConfig

def test_planner_config_defaults():
    cfg = PlannerConfig()
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.max_depth == 3
    assert cfg.plan_label == "symphony:plan"
    assert cfg.leaf_label == "symphony:leaf"
    assert cfg.concept_label == "symphony:concept"
    assert cfg.planned_label == "symphony:planned"
    assert cfg.planner_workspace == "./workspaces/_planner"


def test_workflow_config_planner_defaults_none():
    from symphony.config.schema import WorkflowConfig, TrackerConfig
    cfg = WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="t",
    )
    assert cfg.planner is None


def test_workflow_config_with_planner():
    from symphony.config.schema import WorkflowConfig, TrackerConfig
    cfg = WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="t",
        planner=PlannerConfig(model="claude-opus-4-7", max_depth=2),
    )
    assert cfg.planner is not None
    assert cfg.planner.model == "claude-opus-4-7"
    assert cfg.planner.max_depth == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_config.py::test_planner_config_defaults tests/test_config.py::test_workflow_config_planner_defaults_none tests/test_config.py::test_workflow_config_with_planner -v
```

Expected: FAIL with `ImportError`

- [ ] **Step 3: Add `PlannerConfig` to `symphony/config/schema.py`**

Add after `TriageConfig`:

```python
class PlannerConfig(BaseModel):
    model: str = "claude-sonnet-4-6"
    max_depth: int = 3
    plan_label: str = "symphony:plan"
    leaf_label: str = "symphony:leaf"
    concept_label: str = "symphony:concept"
    planned_label: str = "symphony:planned"
    planner_workspace: str = "./workspaces/_planner"
```

Add to `WorkflowConfig`:

```python
planner: Optional[PlannerConfig] = None
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_config.py -v
```

Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add symphony/config/schema.py tests/test_config.py
git commit -m "feat: add PlannerConfig schema"
```

---

## Task 5: Add GitHub client methods for planner

**Files:**
- Modify: `symphony/tracker/github.py`
- Modify: `tests/test_tracker.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tracker.py` (after the existing imports and helpers — find the last test in the file and add below it):

```python
@pytest.mark.asyncio
async def test_create_issue():
    with respx.mock:
        respx.post("https://api.github.com/repos/owner/repo/issues").mock(
            return_value=httpx.Response(201, json={
                "number": 99, "node_id": "node99", "title": "New child",
                "html_url": "https://github.com/owner/repo/issues/99",
                "state": "open", "labels": [], "body": "desc",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            })
        )
        gh = GitHubClient(_config())
        result = await gh.create_issue("New child", "desc", ["symphony:ready"])
    assert result["number"] == 99
    assert result["node_id"] == "node99"


@pytest.mark.asyncio
async def test_add_sub_issue_success():
    with respx.mock:
        respx.post(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(200, json={}))
        gh = GitHubClient(_config())
        result = await gh.add_sub_issue(42, "node99")
    assert result is True


@pytest.mark.asyncio
async def test_add_sub_issue_unavailable():
    with respx.mock:
        respx.post(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(404, json={}))
        gh = GitHubClient(_config())
        result = await gh.add_sub_issue(42, "node99")
    assert result is False


@pytest.mark.asyncio
async def test_fetch_sub_issues_success():
    with respx.mock:
        respx.get(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(200, json=[
            {"number": 51, "state": "open"},
            {"number": 52, "state": "closed"},
        ]))
        gh = GitHubClient(_config())
        result = await gh.fetch_sub_issues(42)
    assert len(result) == 2
    assert result[0]["number"] == 51


@pytest.mark.asyncio
async def test_fetch_sub_issues_unavailable():
    with respx.mock:
        respx.get(
            "https://api.github.com/repos/owner/repo/issues/42/sub_issues"
        ).mock(return_value=httpx.Response(404, json={}))
        gh = GitHubClient(_config())
        result = await gh.fetch_sub_issues(42)
    assert result == []


@pytest.mark.asyncio
async def test_fetch_issues_by_label():
    issue_data = _gh_issue(number=10, labels=[{"name": "symphony:planned"}])
    with respx.mock:
        respx.get("https://api.github.com/repos/owner/repo/issues").mock(
            return_value=httpx.Response(200, json=[issue_data])
        )
        gh = GitHubClient(_config())
        results = await gh.fetch_issues_by_label("symphony:planned")
    assert len(results) == 1
    assert results[0].number == 10
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_tracker.py::test_create_issue tests/test_tracker.py::test_add_sub_issue_success tests/test_tracker.py::test_fetch_issues_by_label -v
```

Expected: FAIL with `AttributeError` (methods don't exist yet)

- [ ] **Step 3: Add methods to `symphony/tracker/github.py`**

Add after `remove_label`:

```python
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
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_tracker.py -v
```

Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add symphony/tracker/github.py tests/test_tracker.py
git commit -m "feat: add create_issue, add_sub_issue, fetch_sub_issues, fetch_issues_by_label to GitHubClient"
```

---

## Task 6: Implement `PlannerAgent`

**Files:**
- Create: `symphony/planner/__init__.py`
- Create: `symphony/planner/agent.py`
- Create: `tests/test_planner_agent.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_planner_agent.py`:

```python
import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from symphony.planner.agent import PlannerAgent, PlanAssessment, ChildSpec
from symphony.tracker.models import Issue
from symphony.config.schema import CodexConfig, PlannerConfig
from symphony.agent.claude import TurnResult


def _config(**kwargs) -> PlannerConfig:
    return PlannerConfig(**kwargs)


def _codex() -> CodexConfig:
    return CodexConfig()


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Build admin dashboard",
        description="We need a full admin dashboard with user management and analytics.",
        state="active", labels=[], branch_name="symphony/1-build-admin-dashboard",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def _turn(text: str, success: bool = True) -> TurnResult:
    return TurnResult(success=success, usage=None, message=text)


@pytest.mark.asyncio
async def test_assess_leaf_task(tmp_path: Path):
    payload = json.dumps({"type": "leaf", "children": None})
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn(payload))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is not None
    assert result.is_leaf is True
    assert result.children == []


@pytest.mark.asyncio
async def test_assess_concept_with_children(tmp_path: Path):
    payload = json.dumps({
        "type": "concept",
        "children": [
            {"title": "Build user list page", "description": "Create /admin/users", "labels": ["symphony:ready"]},
            {"title": "Build analytics page", "description": "Create /admin/analytics", "labels": ["symphony:ready"]},
        ],
    })
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn(payload))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is not None
    assert result.is_leaf is False
    assert len(result.children) == 2
    assert result.children[0].title == "Build user list page"
    assert "symphony:ready" in result.children[0].labels


@pytest.mark.asyncio
async def test_assess_at_max_depth_returns_leaf(tmp_path: Path):
    agent = PlannerAgent(_config(max_depth=2), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock()) as mock_run:
        result = await agent.assess(_issue(), [], 2, tmp_path)
    assert result is not None
    assert result.is_leaf is True
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_assess_handles_runner_exception(tmp_path: Path):
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(side_effect=Exception("fail"))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_failed_turn(tmp_path: Path):
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn("", success=False))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_handles_bad_json(tmp_path: Path):
    agent = PlannerAgent(_config(), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn("not json"))):
        result = await agent.assess(_issue(), [], 0, tmp_path)
    assert result is None


@pytest.mark.asyncio
async def test_assess_passes_model_to_runner(tmp_path: Path):
    payload = json.dumps({"type": "leaf", "children": None})
    agent = PlannerAgent(_config(model="claude-opus-4-7"), _codex())
    with patch.object(agent._runner, "run_turn", AsyncMock(return_value=_turn(payload))) as mock_run:
        await agent.assess(_issue(), [], 0, tmp_path)
    assert mock_run.call_args[1].get("model") == "claude-opus-4-7"


def test_build_prompt_includes_issue_content():
    agent = PlannerAgent(_config(), _codex())
    issue = _issue(title="Add OAuth", description="Support Google OAuth.")
    prompt = agent._build_prompt(issue, [], 0)
    assert "Add OAuth" in prompt
    assert "Support Google OAuth." in prompt
    assert "depth: 0" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_planner_agent.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `symphony/planner/__init__.py`**

```python
```

(empty file)

- [ ] **Step 4: Create `symphony/planner/agent.py`**

```python
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from symphony.agent.claude import ClaudeRunner
from symphony.config.schema import CodexConfig, PlannerConfig
from symphony.tracker.models import Issue

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a planning agent for an autonomous coding system called Symphony.
Your job is to assess whether a GitHub issue can be implemented directly as a
leaf task, or needs to be broken into smaller child issues first.

A LEAF task is directly implementable when:
- The scope is clearly bounded with a defined "done" state
- An agent can complete it in one focused session
- No coordination with other parallel tasks is required

A CONCEPT needs decomposition when:
- It spans multiple independent components or concerns
- It can be clearly split into independently implementable subtasks

When decomposing, each child must be independently implementable as a leaf task.
Include enough context in each child description for an agent to implement it
without access to the parent issue.

You have access to the repository via your tools. Browse the codebase to inform
your decomposition decisions where helpful.

Respond with a JSON object only — no prose outside the JSON.

For a leaf task:
{"type": "leaf", "children": null}

For a concept:
{
  "type": "concept",
  "children": [
    {
      "title": "Concise imperative title",
      "description": "Full markdown description with acceptance criteria",
      "labels": ["symphony:ready"]
    }
  ]
}\
"""


@dataclass
class ChildSpec:
    title: str
    description: str
    labels: list[str] = field(default_factory=list)


@dataclass
class PlanAssessment:
    is_leaf: bool
    children: list[ChildSpec] = field(default_factory=list)


class PlannerAgent:
    def __init__(self, config: PlannerConfig, codex: CodexConfig) -> None:
        self._config = config
        self._runner = ClaudeRunner(codex)

    def _build_prompt(self, issue: Issue, comments: list[dict], depth: int) -> str:
        parts = [
            f"# Issue #{issue.number}: {issue.title}",
            f"Current depth: {depth} (max allowed: {self._config.max_depth - 1})",
            "",
            "## Body",
            issue.description or "(no description)",
            "",
        ]
        if issue.labels:
            parts += ["## Labels", ", ".join(issue.labels), ""]
        if comments:
            parts += ["## Comments (newest last, up to 20)"]
            for c in comments[-20:]:
                author = c.get("user", {}).get("login", "unknown")
                parts.append(f"**{author}:** {c['body']}")
                parts.append("")
        parts += ["## Instructions", _SYSTEM_PROMPT]
        return "\n".join(parts)

    async def assess(
        self,
        issue: Issue,
        comments: list[dict],
        depth: int,
        workspace: Path,
    ) -> PlanAssessment | None:
        if depth >= self._config.max_depth:
            return PlanAssessment(is_leaf=True)

        prompt = self._build_prompt(issue, comments, depth)
        try:
            result = await self._runner.run_turn(
                workspace=workspace,
                prompt=prompt,
                is_continuation=False,
                model=self._config.model,
            )
        except Exception as exc:
            log.error("Planning API call failed for issue #%d: %s", issue.number, exc)
            return None

        if not result.success:
            log.error("Planning call failed for issue #%d: %s", issue.number, result.message)
            return None

        try:
            data = json.loads(result.message)
            if data["type"] == "leaf":
                return PlanAssessment(is_leaf=True)
            children = [
                ChildSpec(
                    title=c["title"],
                    description=c["description"],
                    labels=c.get("labels", []),
                )
                for c in (data.get("children") or [])
            ]
            return PlanAssessment(is_leaf=False, children=children)
        except Exception as exc:
            log.error(
                "Planning JSON parse failed for issue #%d: %s\nRaw: %s",
                issue.number, exc, result.message,
            )
            return None
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_planner_agent.py -v
```

Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add symphony/planner/__init__.py symphony/planner/agent.py tests/test_planner_agent.py
git commit -m "feat: add PlannerAgent"
```

---

## Task 7: Implement `PlannerRunner`

**Files:**
- Create: `symphony/planner/runner.py`
- Create: `tests/test_planner_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_planner_runner.py`:

```python
import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from symphony.planner.runner import PlannerRunner, _parse_plan_marker, _build_marker, _get_depth
from symphony.planner.agent import PlanAssessment, ChildSpec
from symphony.tracker.models import Issue
from symphony.config.schema import CodexConfig, PlannerConfig


def _config(**kwargs) -> PlannerConfig:
    return PlannerConfig(**kwargs)


def _codex() -> CodexConfig:
    return CodexConfig()


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Big feature",
        description="A high-level concept.",
        state="active", labels=[], branch_name="symphony/1-big-feature",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 2),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def test_parse_plan_marker_valid():
    body = '<!-- symphony-plan {"children": [51, 52], "depth": 0} -->'
    data = _parse_plan_marker(body)
    assert data == {"children": [51, 52], "depth": 0}


def test_parse_plan_marker_not_a_marker():
    assert _parse_plan_marker("not a marker") is None


def test_build_marker_roundtrip():
    marker = _build_marker([51, 52, 53], 1)
    data = _parse_plan_marker(marker)
    assert data["children"] == [51, 52, 53]
    assert data["depth"] == 1


def test_get_depth_no_label():
    issue = _issue(labels=[])
    assert _get_depth(issue) == 0


def test_get_depth_with_label():
    issue = _issue(labels=["symphony:depth:2", "symphony:ready"])
    assert _get_depth(issue) == 2


@pytest.mark.asyncio
async def test_plan_issue_already_planned_skips(tmp_path):
    issue = _issue(labels=["symphony:planned"])
    gh = AsyncMock()
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    await runner.plan_issue(issue, force=False)
    gh.fetch_issue_comments.assert_not_called()


@pytest.mark.asyncio
async def test_plan_issue_already_planned_force_proceeds(tmp_path):
    issue = _issue(labels=["symphony:planned"])
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    assessment = PlanAssessment(is_leaf=True)
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.plan_issue(issue, force=True)
    gh.add_labels.assert_called_once()


@pytest.mark.asyncio
async def test_plan_issue_leaf_applies_leaf_label(tmp_path):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    with patch.object(runner._agent, "assess", AsyncMock(return_value=PlanAssessment(is_leaf=True))):
        await runner.plan_issue(issue)
    gh.add_labels.assert_called_once_with(1, ["symphony:leaf"])
    gh.remove_label.assert_called_once_with(1, "symphony:plan")


@pytest.mark.asyncio
async def test_plan_issue_concept_creates_children_and_labels(tmp_path):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    gh.create_issue.side_effect = [
        {"number": 51, "node_id": "node51"},
        {"number": 52, "node_id": "node52"},
    ]
    gh.add_sub_issue.return_value = True

    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    assessment = PlanAssessment(is_leaf=False, children=[
        ChildSpec(title="Child A", description="Do A", labels=["symphony:ready"]),
        ChildSpec(title="Child B", description="Do B", labels=["symphony:ready"]),
    ])
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.plan_issue(issue)

    assert gh.create_issue.call_count == 2
    assert gh.post_comment.call_count == 1
    comment_body = gh.post_comment.call_args[0][1]
    assert "51" in comment_body
    assert "52" in comment_body

    final_labels = gh.add_labels.call_args_list[-1][0][1]
    assert "symphony:concept" in final_labels
    assert "symphony:planned" in final_labels
    gh.remove_label.assert_called_with(1, "symphony:plan")


@pytest.mark.asyncio
async def test_plan_issue_dry_run_no_github_writes(tmp_path, capsys):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []
    runner = PlannerRunner(_config(), _codex(), gh, dry_run=True)
    runner._workspace = tmp_path
    assessment = PlanAssessment(is_leaf=False, children=[
        ChildSpec(title="Child A", description="Do A", labels=[]),
    ])
    with patch.object(runner._agent, "assess", AsyncMock(return_value=assessment)):
        await runner.plan_issue(issue)

    gh.create_issue.assert_not_called()
    gh.post_comment.assert_not_called()
    captured = capsys.readouterr()
    assert "Child A" in captured.out


@pytest.mark.asyncio
async def test_get_child_numbers_from_marker(tmp_path):
    issue = _issue()
    gh = AsyncMock()
    marker = _build_marker([51, 52, 53], 0)
    gh.fetch_issue_comments.return_value = [
        {"body": "some other comment"},
        {"body": marker},
    ]
    runner = PlannerRunner(_config(), _codex(), gh)
    runner._workspace = tmp_path
    numbers = await runner.get_child_numbers(issue)
    assert numbers == [51, 52, 53]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_planner_runner.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Create `symphony/planner/runner.py`**

```python
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from symphony.config.schema import CodexConfig, PlannerConfig
from symphony.tracker.github import GitHubClient
from symphony.tracker.models import Issue
from symphony.planner.agent import PlannerAgent

log = logging.getLogger(__name__)

_MARKER_PREFIX = "<!-- symphony-plan "
_MARKER_SUFFIX = " -->"


def _parse_plan_marker(body: str) -> Optional[dict]:
    if not body.startswith(_MARKER_PREFIX):
        return None
    end = body.find(_MARKER_SUFFIX, len(_MARKER_PREFIX))
    if end == -1:
        return None
    try:
        return json.loads(body[len(_MARKER_PREFIX):end])
    except Exception:
        return None


def _build_marker(children: list[int], depth: int) -> str:
    data = json.dumps({"children": children, "depth": depth})
    return f"{_MARKER_PREFIX}{data}{_MARKER_SUFFIX}"


def _get_depth(issue: Issue) -> int:
    for label in issue.labels:
        if label.startswith("symphony:depth:"):
            try:
                return int(label.split(":")[-1])
            except ValueError:
                pass
    return 0


class PlannerRunner:
    def __init__(
        self,
        config: PlannerConfig,
        codex: CodexConfig,
        client: GitHubClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._client = client
        self._agent = PlannerAgent(config, codex)
        self._dry_run = dry_run
        self._workspace = Path(config.planner_workspace)
        self._sub_issues_available: Optional[bool] = None

    async def _try_add_sub_issue(self, parent_number: int, child_node_id: str) -> None:
        if self._sub_issues_available is False:
            return
        result = await self._client.add_sub_issue(parent_number, child_node_id)
        self._sub_issues_available = result

    async def plan_issue(self, issue: Issue, force: bool = False) -> None:
        if self._config.planned_label in issue.labels and not force:
            log.info("Issue #%d already planned, skipping (use --force to re-plan)", issue.number)
            return

        depth = _get_depth(issue)
        self._workspace.mkdir(parents=True, exist_ok=True)

        comments = await self._client.fetch_issue_comments(issue.number)
        assessment = await self._agent.assess(issue, comments, depth, self._workspace)

        if assessment is None:
            log.warning("Skipping issue #%d due to assessment failure", issue.number)
            return

        if assessment.is_leaf:
            log.info("Issue #%d classified as leaf task", issue.number)
            if self._dry_run:
                print(f"\n--- Issue #{issue.number}: {issue.title} ---")
                print("Type: leaf")
                return
            await self._client.add_labels(issue.number, [self._config.leaf_label])
            await self._client.remove_label(issue.number, self._config.plan_label)
            return

        log.info(
            "Issue #%d classified as concept, decomposing into %d children",
            issue.number, len(assessment.children),
        )

        if self._dry_run:
            print(f"\n--- Issue #{issue.number}: {issue.title} ---")
            print("Type: concept")
            for i, child in enumerate(assessment.children):
                print(f"\nChild {i + 1}: {child.title}")
                print(f"Labels: {child.labels}")
                print(f"Description:\n{child.description}")
            return

        child_numbers: list[int] = []
        child_depth = depth + 1
        depth_label = f"symphony:depth:{child_depth}"

        for child_spec in assessment.children:
            child_labels = list(child_spec.labels) + [depth_label]
            child_issue = await self._client.create_issue(
                title=child_spec.title,
                body=f"_Decomposed from #{issue.number}_\n\n{child_spec.description}",
                labels=child_labels,
            )
            child_numbers.append(child_issue["number"])
            await self._try_add_sub_issue(issue.number, child_issue["node_id"])

        marker = _build_marker(child_numbers, depth)
        await self._client.post_comment(issue.number, marker)
        await self._client.add_labels(
            issue.number,
            [self._config.concept_label, self._config.planned_label],
        )
        await self._client.remove_label(issue.number, self._config.plan_label)

    async def get_child_numbers(self, issue: Issue) -> list[int]:
        comments = await self._client.fetch_issue_comments(issue.number)
        for comment in reversed(comments):
            data = _parse_plan_marker(comment["body"])
            if data is not None:
                return data.get("children", [])
        return []

    async def run(self, issues: list[Issue], force: bool = False) -> None:
        for issue in issues:
            await self.plan_issue(issue, force=force)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_planner_runner.py -v
```

Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add symphony/planner/runner.py tests/test_planner_runner.py
git commit -m "feat: add PlannerRunner"
```

---

## Task 8: Wire planner into the orchestrator

**Files:**
- Modify: `symphony/orchestrator/core.py`
- Modify: `tests/test_orchestrator.py`

The orchestrator gains two new capabilities:
1. In `_tick()`: detect issues with `symphony:plan` label via a separate fetch and dispatch them to `PlannerRunner`
2. A new `_watch_planned()` loop: poll for `symphony:planned` issues and close the parent when all children are terminal

The orchestrator creates a `GitHubClient` internally when `config.planner` is set, since label-management operations require GitHub-specific methods not on the `TrackerClient` base.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_orchestrator.py`:

```python
from symphony.config.schema import PlannerConfig

def _config_with_planner(**kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        tracker=TrackerConfig(kind="github", repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
        planner=PlannerConfig(**kwargs),
    )

def _plan_issue(number=10) -> Issue:
    return Issue(
        id=f"plan{number}", identifier=f"o/r#{number}", number=number,
        title="Big concept", description="", state="active",
        labels=["symphony:plan"], branch_name=f"symphony/{number}-big-concept",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1),
    )

@pytest.mark.asyncio
async def test_tick_dispatches_plan_issues_to_planner():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []
    tracker.fetch_issues_by_label.return_value = [_plan_issue()]

    orch = Orchestrator(_config_with_planner(), tracker)
    planned = []

    async def _fake_plan(issue, force=False):
        planned.append(issue.number)

    with patch("symphony.orchestrator.core.PlannerRunner") as MockRunner:
        instance = MockRunner.return_value
        instance.plan_issue = AsyncMock(side_effect=_fake_plan)
        await orch._tick()
        await asyncio.sleep(0)  # let tasks run

    assert 10 in planned or MockRunner.called


@pytest.mark.asyncio
async def test_tick_skips_planner_when_not_configured():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []
    tracker.fetch_candidate_issues.return_value = []
    tracker.fetch_issues_by_numbers.return_value = []

    orch = Orchestrator(_config(), tracker)
    # Should not call fetch_issues_by_label when planner is not configured
    await orch._tick()
    tracker.fetch_issues_by_label.assert_not_called()


@pytest.mark.asyncio
async def test_watch_planned_closes_parent_when_all_children_done():
    tracker = AsyncMock()
    tracker.fetch_terminal_issues.return_value = []

    parent = _issue(id_="parent1", number=42, state="active")
    parent.labels = ["symphony:planned"]
    child1 = _issue(id_="c1", number=51, state="terminal")
    child2 = _issue(id_="c2", number=52, state="terminal")

    tracker.fetch_issues_by_label.return_value = [parent]
    tracker.fetch_issues_by_numbers.return_value = [child1, child2]

    orch = Orchestrator(_config_with_planner(), tracker)

    with patch("symphony.orchestrator.core.PlannerRunner") as MockRunner:
        instance = MockRunner.return_value
        instance.get_child_numbers = AsyncMock(return_value=[51, 52])
        instance.plan_issue = AsyncMock()
        # Patch the github client calls
        with patch.object(orch, "_gh_add_labels", AsyncMock()) as mock_add, \
             patch.object(orch, "_gh_remove_label", AsyncMock()) as mock_remove:
            await orch._watch_planned_tick()

    mock_add.assert_called_once()
    mock_remove.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_orchestrator.py::test_tick_dispatches_plan_issues_to_planner tests/test_orchestrator.py::test_tick_skips_planner_when_not_configured tests/test_orchestrator.py::test_watch_planned_closes_parent_when_all_children_done -v
```

Expected: FAIL

- [ ] **Step 3: Update `symphony/orchestrator/core.py`**

Add imports at the top:

```python
from symphony.config.schema import WorkflowConfig
```

Replace with expanded imports:

```python
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from symphony.config.schema import WorkflowConfig
from symphony.orchestrator.dispatch import is_eligible, sort_issues, retry_delay_ms
from symphony.orchestrator.state import (
    LiveSession, OrchestratorState, RetryEntry, TokenTotals,
)
from symphony.tracker.base import TrackerClient
from symphony.tracker.models import Issue
from symphony.worker.local import LocalWorker
from symphony.worker.ssh import SSHWorker
from symphony.workspace.manager import WorkspaceManager
```

Add to `Orchestrator.__init__` after existing init code:

```python
self._planner_runner = None
self._github = None
if config.planner:
    from symphony.tracker.github import GitHubClient
    from symphony.planner.runner import PlannerRunner
    self._github = GitHubClient(config.tracker)
    self._planner_runner = PlannerRunner(config.planner, config.codex, self._github)
```

Add helper methods for GitHub label operations (called by `_watch_planned_tick`):

```python
async def _gh_add_labels(self, number: int, labels: list[str]) -> None:
    if self._github:
        await self._github.add_labels(number, labels)

async def _gh_remove_label(self, number: int, label: str) -> None:
    if self._github:
        await self._github.remove_label(number, label)
```

Update `run()` to launch `_watch_planned()` when planner is configured:

```python
async def run(self) -> None:
    await self._startup_cleanup()
    tasks = [asyncio.create_task(self._tick_loop())]
    if self._config.planner:
        tasks.append(asyncio.create_task(self._watch_planned()))
    await asyncio.gather(*tasks)

async def _tick_loop(self) -> None:
    while True:
        self._refresh_event.clear()
        await self._tick()
        try:
            await asyncio.wait_for(
                self._refresh_event.wait(),
                timeout=self._config.polling.interval_ms / 1000,
            )
        except asyncio.TimeoutError:
            pass
```

Add plan dispatch to `_tick()` — insert before the existing dispatch loop:

```python
async def _tick(self) -> None:
    await self._reconcile()
    await self._fire_retries()

    # Dispatch plan-labeled issues to the planner
    if self._config.planner and self._planner_runner:
        try:
            plan_issues = await self._tracker.fetch_issues_by_label(
                self._config.planner.plan_label
            )
            async with self._lock:
                for issue in plan_issues:
                    if issue.id not in self._state.claimed:
                        self._state.claimed.add(issue.id)
                        asyncio.create_task(self._run_planner(issue))
        except Exception as e:
            logger.warning("Plan issue fetch failed: %s", e)

    try:
        issues = await self._tracker.fetch_candidate_issues()
    except Exception as e:
        logger.warning("Candidate fetch failed, skipping dispatch: %s", e)
        return
    sorted_issues = sort_issues(issues)
    async with self._lock:
        for issue in sorted_issues:
            if not is_eligible(issue, self._state, self._config):
                continue
            self._state.claimed.add(issue.id)
            task = asyncio.create_task(self._run_worker(issue, attempt=None))
            self._state.running[issue.id] = LiveSession(issue=issue, task=task)
```

Add `_run_planner`, `_watch_planned`, and `_watch_planned_tick` methods:

```python
async def _run_planner(self, issue: Issue) -> None:
    try:
        await self._planner_runner.plan_issue(issue)
    except Exception as e:
        logger.error("Planner failed for issue #%d: %s", issue.number, e)
    finally:
        async with self._lock:
            self._state.claimed.discard(issue.id)

async def _watch_planned(self) -> None:
    while True:
        try:
            await self._watch_planned_tick()
        except Exception as e:
            logger.warning("watch_planned tick failed: %s", e)
        await asyncio.sleep(self._config.polling.interval_ms / 1000)

async def _watch_planned_tick(self) -> None:
    if not self._config.planner or not self._planner_runner:
        return
    planned_issues = await self._tracker.fetch_issues_by_label(
        self._config.planner.planned_label
    )
    for issue in planned_issues:
        child_numbers = await self._planner_runner.get_child_numbers(issue)
        if not child_numbers:
            continue
        children = await self._tracker.fetch_issues_by_numbers(child_numbers)
        if not children:
            continue
        all_terminal = all(c.state == "terminal" for c in children)
        if all_terminal:
            logger.info(
                "All children of issue #%d complete, closing parent", issue.number
            )
            terminal_label = self._config.tracker.terminal_labels[0]
            await self._gh_add_labels(issue.number, [terminal_label])
            await self._gh_remove_label(issue.number, self._config.planner.planned_label)
```

Also add `fetch_issues_by_label` to the `TrackerClient` abstract base in `symphony/tracker/base.py` so the tracker mock in tests supports it:

```python
# symphony/tracker/base.py — add:
@abstractmethod
async def fetch_issues_by_label(self, label: str) -> list[Issue]: ...
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_orchestrator.py -v
```

Expected: All PASS

- [ ] **Step 5: Run full suite**

```bash
pytest -v
```

Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add symphony/orchestrator/core.py symphony/tracker/base.py tests/test_orchestrator.py
git commit -m "feat: wire planner into orchestrator — plan label dispatch and watch_planned loop"
```

---

## Task 9: Add `symphony plan` CLI command

**Files:**
- Modify: `symphony/main.py`

- [ ] **Step 1: Add `_plan` function and subparser to `symphony/main.py`**

Add the `_plan` async function after `_triage`:

```python
async def _plan(
    workflow_path: Path,
    issue_numbers: list[int] | None,
    dry_run: bool,
    force: bool,
) -> None:
    from symphony.config.loader import load_workflow
    from symphony.config.schema import PlannerConfig
    from symphony.tracker.github import GitHubClient
    from symphony.planner.runner import PlannerRunner

    config = load_workflow(workflow_path)
    if not config.planner:
        print("Error: planner is not configured in WORKFLOW.md. Add a [planner] section.", file=sys.stderr)
        sys.exit(1)

    tracker = GitHubClient(config.tracker)
    runner = PlannerRunner(config.planner, config.codex, tracker, dry_run=dry_run)

    if not issue_numbers:
        print("Error: --issue is required for symphony plan.", file=sys.stderr)
        sys.exit(1)

    issues = await tracker.fetch_issues_by_numbers(issue_numbers)
    await runner.run(issues, force=force)
```

Add the subparser in `main()` after the triage subparser block:

```python
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
```

Add the dispatch block in `main()` after the triage dispatch:

```python
if args.command == "plan":
    _setup_logging(args.log_level)
    issue_numbers = [int(n.strip()) for n in args.issues.split(",")]
    asyncio.run(_plan(Path(args.workflow), issue_numbers, args.dry_run, args.force))
    return
```

- [ ] **Step 2: Verify CLI help works**

```bash
python -m symphony.main plan --help
```

Expected output includes `--issue`, `--dry-run`, `--force`, `--log-level`

- [ ] **Step 3: Run the full test suite**

```bash
pytest -v
```

Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add symphony/main.py
git commit -m "feat: add symphony plan CLI subcommand"
```

---

## Self-Review Checklist

After all tasks complete, run:

```bash
pytest -v --tb=short
```

Verify:
- `tests/test_agent.py` — ClaudeRunner model flag tests pass
- `tests/test_triage_agent.py` — Refactored TriageAgent tests pass
- `tests/test_triage_runner.py` — Updated runner tests pass
- `tests/test_config.py` — PlannerConfig tests pass
- `tests/test_tracker.py` — New GitHub client method tests pass
- `tests/test_planner_agent.py` — PlannerAgent tests pass
- `tests/test_planner_runner.py` — PlannerRunner tests pass
- `tests/test_orchestrator.py` — Orchestrator planner tests pass
- No `anthropic` imports remain anywhere in `symphony/` or `tests/`
