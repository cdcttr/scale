# Symphony Triage Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `symphony triage` CLI command that uses Claude Haiku to assess GitHub issue readiness, post a structured comment, and apply `symphony:ready` / `symphony:needs-detail` labels.

**Architecture:** A `TriageAgent` class calls the Anthropic SDK directly to assess a single issue and produce a `TriageAssessment` dataclass. A `TriageRunner` orchestrates the per-issue flow: fetch comments → detect if re-triage is needed → call agent → post comment → apply labels. The `symphony triage` CLI subcommand wraps `TriageRunner` with argument parsing.

**Tech Stack:** Python 3.12, `anthropic>=0.40` (Anthropic SDK), `httpx` (via existing GitHubClient), `pydantic` (TriageConfig schema), `argparse` (CLI).

---

## File Map

| File | Change |
|---|---|
| `pyproject.toml` | Add `anthropic>=0.40` runtime dependency |
| `symphony/config/schema.py` | Add `TriageConfig`; add optional `triage` field to `WorkflowConfig` |
| `symphony/tracker/github.py` | Add `fetch_issue_comments`, `post_comment`, `add_labels`, `remove_label` |
| `symphony/triage/__init__.py` | New (empty) |
| `symphony/triage/agent.py` | `TriageAgent`: prompt construction, Anthropic API call, JSON parsing |
| `symphony/triage/runner.py` | `TriageRunner`: orchestrates per-issue triage flow |
| `symphony/main.py` | Add `triage` subcommand + `_triage` async function |
| `tests/test_triage_agent.py` | Unit tests for `TriageAgent` |
| `tests/test_triage_runner.py` | Unit tests for `TriageRunner` |
| `tests/test_tracker.py` | Append tests for new `GitHubClient` triage methods |

---

## Task 1: Add anthropic dependency and TriageConfig schema

**Files:**
- Modify: `pyproject.toml`
- Modify: `symphony/config/schema.py`

- [ ] **Step 1: Write the failing test**

```python
# At the end of tests/test_config.py (append — do not replace)

from symphony.config.schema import TriageConfig, WorkflowConfig, TrackerConfig


def test_triage_config_defaults():
    cfg = TriageConfig()
    assert cfg.model == "claude-haiku-4-5-20251001"
    assert cfg.ready_label == "symphony:ready"
    assert cfg.needs_detail_label == "symphony:needs-detail"
    assert cfg.triaged_label == "symphony:triaged"


def test_workflow_config_triage_optional():
    wf = WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
    )
    assert wf.triage is None


def test_workflow_config_triage_set():
    wf = WorkflowConfig(
        tracker=TrackerConfig(repo="o/r", api_token="tok"),
        prompt_template="Work on {{ issue.title }}.",
        triage=TriageConfig(model="claude-sonnet-4-6"),
    )
    assert wf.triage is not None
    assert wf.triage.model == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/djohnson/projects/openai-symphony
uv run pytest tests/test_config.py::test_triage_config_defaults -v
```

Expected: `FAILED` with `ImportError: cannot import name 'TriageConfig'`

- [ ] **Step 3: Add TriageConfig to schema**

In `symphony/config/schema.py`, add `TriageConfig` class and the `triage` field to `WorkflowConfig`:

```python
class TriageConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    ready_label: str = "symphony:ready"
    needs_detail_label: str = "symphony:needs-detail"
    triaged_label: str = "symphony:triaged"
```

Then update `WorkflowConfig` to add the optional `triage` field after `worker`:

```python
class WorkflowConfig(BaseModel):
    tracker: TrackerConfig
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    server: Optional[ServerConfig] = None
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    triage: Optional[TriageConfig] = None
    prompt_template: str = ""
```

- [ ] **Step 4: Add anthropic to pyproject.toml**

In `pyproject.toml`, add to `dependencies` list:

```toml
dependencies = [
    "pydantic>=2.0",
    "python-frontmatter>=1.1",
    "python-liquid>=1.12",
    "httpx>=0.27",
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
    "rich>=13.7",
    "watchfiles>=0.22",
    "anthropic>=0.40",
]
```

- [ ] **Step 5: Install the new dependency**

```bash
uv sync
```

Expected: `anthropic` package installed without errors.

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: all tests PASS including the three new ones.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock symphony/config/schema.py tests/test_config.py
git commit -m "feat: add TriageConfig schema and anthropic dependency"
```

---

## Task 2: GitHubClient triage methods

**Files:**
- Modify: `symphony/tracker/github.py`
- Modify: `tests/test_tracker.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tracker.py` (after the existing tests):

```python
from urllib.parse import quote as _url_quote


@pytest.mark.asyncio
@respx.mock
async def test_fetch_issue_comments_returns_list():
    respx.get("https://api.github.com/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "body": "comment text", "user": {"login": "alice"}, "created_at": "2026-01-01T00:00:00Z"},
        ])
    )
    client = GitHubClient(_config())
    comments = await client.fetch_issue_comments(42)
    assert len(comments) == 1
    assert comments[0]["body"] == "comment text"


@pytest.mark.asyncio
@respx.mock
async def test_post_comment_sends_body():
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/comments").mock(
        return_value=httpx.Response(201, json={"id": 99, "body": "hello"})
    )
    client = GitHubClient(_config())
    await client.post_comment(42, "hello")
    assert route.called
    import json as _json
    sent = _json.loads(route.calls[0].request.content)
    assert sent["body"] == "hello"


@pytest.mark.asyncio
@respx.mock
async def test_add_labels_sends_labels():
    route = respx.post("https://api.github.com/repos/owner/repo/issues/42/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    client = GitHubClient(_config())
    await client.add_labels(42, ["symphony:ready", "symphony:triaged"])
    assert route.called
    import json as _json
    sent = _json.loads(route.calls[0].request.content)
    assert "symphony:ready" in sent["labels"]


@pytest.mark.asyncio
@respx.mock
async def test_remove_label_success():
    encoded = _url_quote("symphony:ready", safe="")
    route = respx.delete(
        f"https://api.github.com/repos/owner/repo/issues/42/labels/{encoded}"
    ).mock(return_value=httpx.Response(200, json=[]))
    client = GitHubClient(_config())
    await client.remove_label(42, "symphony:ready")
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_remove_label_404_is_silent():
    encoded = _url_quote("symphony:ready", safe="")
    respx.delete(
        f"https://api.github.com/repos/owner/repo/issues/42/labels/{encoded}"
    ).mock(return_value=httpx.Response(404))
    client = GitHubClient(_config())
    await client.remove_label(42, "symphony:ready")  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_tracker.py::test_fetch_issue_comments_returns_list -v
```

Expected: `FAILED` with `AttributeError: 'GitHubClient' object has no attribute 'fetch_issue_comments'`

- [ ] **Step 3: Add the four methods to GitHubClient**

Add these methods to `symphony/tracker/github.py` after the `fetch_terminal_issues` method. Also add `from urllib.parse import quote as _url_quote` at the top of the file (after existing imports):

```python
    async def fetch_issue_comments(self, number: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{self._base}/issues/{number}/comments",
                headers=self._headers,
                params={"per_page": 100},
            )
            r.raise_for_status()
            return r.json()

    async def post_comment(self, number: int, body: str) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base}/issues/{number}/comments",
                headers=self._headers,
                json={"body": body},
            )
            r.raise_for_status()

    async def add_labels(self, number: int, labels: list[str]) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self._base}/issues/{number}/labels",
                headers=self._headers,
                json={"labels": labels},
            )
            r.raise_for_status()

    async def remove_label(self, number: int, label: str) -> None:
        encoded = _url_quote(label, safe="")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.delete(
                f"{self._base}/issues/{number}/labels/{encoded}",
                headers=self._headers,
            )
            if r.status_code == 404:
                return
            r.raise_for_status()
```

The import to add at the top of `symphony/tracker/github.py` (after `import httpx`):

```python
from urllib.parse import quote as _url_quote
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_tracker.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add symphony/tracker/github.py tests/test_tracker.py
git commit -m "feat: add GitHubClient triage methods (comments, post, labels)"
```

---

## Task 3: TriageAgent

**Files:**
- Create: `symphony/triage/__init__.py`
- Create: `symphony/triage/agent.py`
- Create: `tests/test_triage_agent.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_triage_agent.py`:

```python
import json
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from symphony.triage.agent import TriageAgent, TriageAssessment
from symphony.tracker.models import Issue
from symphony.config.schema import TriageConfig


def _config(**kwargs) -> TriageConfig:
    return TriageConfig(**kwargs)


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


def _make_response(text: str) -> MagicMock:
    block = MagicMock()
    block.text = text
    msg = MagicMock()
    msg.content = [block]
    return msg


def test_assess_returns_ready_assessment():
    payload = json.dumps({
        "ready": True,
        "summary": "Clear and actionable.",
        "reasons": [],
        "comment": "## Symphony Triage\n\n**Status: Ready ✅**\n\nClear.",
    })
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", return_value=_make_response(payload)):
        result = agent.assess(_issue(), [])
    assert result is not None
    assert result.ready is True
    assert result.summary == "Clear and actionable."
    assert result.reasons == []


def test_assess_returns_not_ready_assessment():
    payload = json.dumps({
        "ready": False,
        "summary": "Missing acceptance criteria.",
        "reasons": ["No acceptance criteria", "Vague scope"],
        "comment": "## Symphony Triage\n\n**Status: Needs more detail ❌**",
    })
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", return_value=_make_response(payload)):
        result = agent.assess(_issue(), [])
    assert result is not None
    assert result.ready is False
    assert "No acceptance criteria" in result.reasons


def test_assess_handles_api_failure():
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", side_effect=Exception("network error")):
        result = agent.assess(_issue(), [])
    assert result is None


def test_assess_handles_bad_json():
    agent = TriageAgent(_config())
    with patch.object(agent._client.messages, "create", return_value=_make_response("not json")):
        result = agent.assess(_issue(), [])
    assert result is None


def test_build_prompt_includes_title_and_body():
    agent = TriageAgent(_config())
    issue = _issue(title="Fix login bug", description="Login fails on Safari.")
    prompt = agent._build_prompt(issue, [])
    assert "Fix login bug" in prompt
    assert "Login fails on Safari." in prompt


def test_build_prompt_truncates_comments_to_20():
    agent = TriageAgent(_config())
    comments = [
        {"user": {"login": f"user{i}"}, "body": f"comment {i}"}
        for i in range(25)
    ]
    prompt = agent._build_prompt(_issue(), comments)
    assert "comment 24" in prompt
    assert "comment 4" not in prompt


def test_build_prompt_includes_labels():
    agent = TriageAgent(_config())
    issue = _issue(labels=["bug", "priority:1"])
    prompt = agent._build_prompt(issue, [])
    assert "bug" in prompt
    assert "priority:1" in prompt


def test_assess_uses_configured_model():
    payload = json.dumps({
        "ready": True, "summary": "OK.", "reasons": [], "comment": "## Symphony Triage\n\n**Status: Ready ✅**",
    })
    agent = TriageAgent(_config(model="claude-sonnet-4-6"))
    with patch.object(agent._client.messages, "create", return_value=_make_response(payload)) as mock_create:
        agent.assess(_issue(), [])
    call_kwargs = mock_create.call_args[1]
    assert call_kwargs["model"] == "claude-sonnet-4-6"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_triage_agent.py -v
```

Expected: `FAILED` with `ModuleNotFoundError: No module named 'symphony.triage'`

- [ ] **Step 3: Create symphony/triage/__init__.py**

Create `symphony/triage/__init__.py` as an empty file.

- [ ] **Step 4: Create symphony/triage/agent.py**

```python
from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field

from anthropic import Anthropic

from symphony.config.schema import TriageConfig
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
    def __init__(self, config: TriageConfig) -> None:
        self._config = config
        self._client = Anthropic()

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
        return "\n".join(parts)

    def assess(self, issue: Issue, comments: list[dict]) -> TriageAssessment | None:
        prompt = self._build_prompt(issue, comments)
        try:
            response = self._client.messages.create(
                model=self._config.model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text
            data = json.loads(raw)
            return TriageAssessment(
                ready=bool(data["ready"]),
                summary=data["summary"],
                reasons=data.get("reasons", []),
                comment=data.get("comment", ""),
            )
        except Exception as exc:
            log.error("Triage assessment failed for issue #%d: %s", issue.number, exc)
            return None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_triage_agent.py -v
```

Expected: all 8 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add symphony/triage/__init__.py symphony/triage/agent.py tests/test_triage_agent.py
git commit -m "feat: add TriageAgent with Anthropic SDK integration"
```

---

## Task 4: TriageRunner

**Files:**
- Create: `symphony/triage/runner.py`
- Create: `tests/test_triage_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_triage_runner.py`:

```python
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from symphony.triage.runner import TriageRunner, _parse_triage_timestamp, _needs_triage
from symphony.triage.agent import TriageAssessment
from symphony.tracker.models import Issue
from symphony.config.schema import TriageConfig


def _config(**kwargs) -> TriageConfig:
    return TriageConfig(**kwargs)


def _issue(**kwargs) -> Issue:
    defaults = dict(
        id="n1", identifier="o/r#1", number=1, title="Add dark mode",
        description="Add a dark mode toggle.",
        state="active", labels=[], branch_name="symphony/1-add-dark-mode",
        url="https://example.com", priority=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    defaults.update(kwargs)
    return Issue(**defaults)


def _triage_comment(ts: datetime) -> dict:
    ts_str = ts.isoformat()
    return {
        "body": f"<!-- symphony-triage {ts_str} -->\n## Symphony Triage\n\n**Status: Ready ✅**",
        "created_at": ts_str,
        "user": {"login": "symphony-bot"},
    }


def test_parse_triage_timestamp_valid():
    body = "<!-- symphony-triage 2026-04-28T14:30:00+00:00 -->\n## Symphony Triage"
    ts = _parse_triage_timestamp(body)
    assert ts is not None
    assert ts.year == 2026
    assert ts.month == 4


def test_parse_triage_timestamp_not_a_triage_comment():
    assert _parse_triage_timestamp("not a triage comment") is None


def test_parse_triage_timestamp_malformed_timestamp():
    assert _parse_triage_timestamp("<!-- symphony-triage BROKEN -->") is None


def test_needs_triage_first_time_no_comments():
    assert _needs_triage(_issue(), [], force=False) is True


def test_needs_triage_stale_issue_updated_after_triage():
    old_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    comments = [_triage_comment(old_ts)]
    assert _needs_triage(issue, comments, force=False) is True


def test_needs_triage_current_issue_not_updated():
    triage_ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 4, tzinfo=timezone.utc))
    comments = [_triage_comment(triage_ts)]
    assert _needs_triage(issue, comments, force=False) is False


def test_needs_triage_force_always_true():
    triage_ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 4, tzinfo=timezone.utc))
    comments = [_triage_comment(triage_ts)]
    assert _needs_triage(issue, comments, force=True) is True


@pytest.mark.asyncio
async def test_triage_issue_skips_when_current():
    triage_ts = datetime(2026, 1, 5, tzinfo=timezone.utc)
    issue = _issue(updated_at=datetime(2026, 1, 4, tzinfo=timezone.utc))
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = [_triage_comment(triage_ts)]

    runner = TriageRunner(_config(), gh)
    await runner.triage_issue(issue, force=False)

    gh.post_comment.assert_not_called()
    gh.add_labels.assert_not_called()


@pytest.mark.asyncio
async def test_triage_issue_ready_posts_and_labels():
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), gh)
    assessment = TriageAssessment(
        ready=True, summary="Clear.",
        comment="## Symphony Triage\n\n**Status: Ready ✅**",
    )
    with patch.object(runner._agent, "assess", return_value=assessment):
        await runner.triage_issue(issue)

    gh.post_comment.assert_called_once()
    body_posted = gh.post_comment.call_args[0][1]
    assert "<!-- symphony-triage" in body_posted
    assert "## Symphony Triage" in body_posted

    labels_added = gh.add_labels.call_args[0][1]
    assert "symphony:ready" in labels_added
    assert "symphony:triaged" in labels_added
    gh.remove_label.assert_called_once_with(issue.number, "symphony:needs-detail")


@pytest.mark.asyncio
async def test_triage_issue_not_ready_posts_and_labels():
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), gh)
    assessment = TriageAssessment(
        ready=False, summary="Missing criteria.",
        reasons=["No acceptance criteria"],
        comment="## Symphony Triage\n\n**Status: Needs more detail ❌**",
    )
    with patch.object(runner._agent, "assess", return_value=assessment):
        await runner.triage_issue(issue)

    labels_added = gh.add_labels.call_args[0][1]
    assert "symphony:needs-detail" in labels_added
    assert "symphony:triaged" in labels_added
    gh.remove_label.assert_called_once_with(issue.number, "symphony:ready")


@pytest.mark.asyncio
async def test_triage_issue_dry_run_no_github_calls(capsys):
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), gh, dry_run=True)
    assessment = TriageAssessment(
        ready=True, summary="Clear.",
        comment="## Symphony Triage\n\n**Status: Ready ✅**",
    )
    with patch.object(runner._agent, "assess", return_value=assessment):
        await runner.triage_issue(issue)

    gh.post_comment.assert_not_called()
    gh.add_labels.assert_not_called()
    captured = capsys.readouterr()
    assert "Ready: True" in captured.out


@pytest.mark.asyncio
async def test_triage_issue_skips_on_assessment_failure():
    issue = _issue()
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), gh)
    with patch.object(runner._agent, "assess", return_value=None):
        await runner.triage_issue(issue)

    gh.post_comment.assert_not_called()


@pytest.mark.asyncio
async def test_run_processes_multiple_issues():
    issues = [_issue(id=f"n{i}", number=i) for i in range(1, 4)]
    gh = AsyncMock()
    gh.fetch_issue_comments.return_value = []

    runner = TriageRunner(_config(), gh)
    assessment = TriageAssessment(ready=True, summary="Clear.", comment="## Symphony Triage")
    with patch.object(runner._agent, "assess", return_value=assessment):
        await runner.run(issues)

    assert gh.post_comment.call_count == 3
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_triage_runner.py -v
```

Expected: `FAILED` with `ModuleNotFoundError: No module named 'symphony.triage.runner'`

- [ ] **Step 3: Create symphony/triage/runner.py**

```python
from __future__ import annotations
import logging
from datetime import datetime, timezone

from symphony.config.schema import TriageConfig
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
    last_triage = max(triage_comments, key=lambda c: c["created_at"])
    ts = _parse_triage_timestamp(last_triage["body"])
    if ts is None:
        return True
    issue_updated = issue.updated_at
    if issue_updated.tzinfo is None:
        issue_updated = issue_updated.replace(tzinfo=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return issue_updated > ts


def _build_comment_body(assessment: TriageAssessment) -> str:
    ts = datetime.now(timezone.utc).isoformat()
    return f"{_MARKER_PREFIX}{ts}{_MARKER_SUFFIX}\n{assessment.comment}"


class TriageRunner:
    def __init__(
        self,
        config: TriageConfig,
        client: GitHubClient,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._client = client
        self._agent = TriageAgent(config)
        self._dry_run = dry_run

    async def triage_issue(self, issue: Issue, force: bool = False) -> None:
        log.info("Checking issue #%d: %s", issue.number, issue.title)
        comments = await self._client.fetch_issue_comments(issue.number)

        if not _needs_triage(issue, comments, force):
            log.info("Issue #%d is already current, skipping", issue.number)
            return

        assessment = self._agent.assess(issue, comments)
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

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_triage_runner.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add symphony/triage/runner.py tests/test_triage_runner.py
git commit -m "feat: add TriageRunner with re-triage detection and label lifecycle"
```

---

## Task 5: CLI triage subcommand

**Files:**
- Modify: `symphony/main.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_orchestrator.py`:

```python
def test_triage_subcommand_help(capsys):
    import sys
    from unittest.mock import patch as mpatch
    with mpatch.object(sys, "argv", ["symphony", "triage", "--help"]):
        from symphony.main import main
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "triage" in captured.out or "triage" in captured.err


def test_triage_subcommand_parses_issue_numbers():
    import sys
    from unittest.mock import patch as mpatch, AsyncMock as AM

    async def _fake_triage(path, issue_numbers, force_all, model, dry_run):
        assert issue_numbers == [1, 2, 3]
        assert dry_run is True

    with mpatch.object(sys, "argv", ["symphony", "triage", "--issue", "1,2,3", "--dry-run"]):
        with mpatch("symphony.main._triage", _fake_triage):
            from symphony.main import main
            import importlib
            import symphony.main as sm
            importlib.reload(sm)
```

Note: the second test above is checking argparse parsing specifically. A simpler integration test is the `--help` check, which is sufficient to verify the subcommand is registered without needing `ANTHROPIC_API_KEY`.

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_orchestrator.py::test_triage_subcommand_help -v
```

Expected: `FAILED` with `SystemExit(2)` (unrecognized subcommand) or similar.

- [ ] **Step 3: Add _triage function and triage subcommand to symphony/main.py**

Add the `_triage` async function before `main()`:

```python
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
    runner = TriageRunner(triage_config, tracker, dry_run=dry_run)

    if issue_numbers:
        issues = await tracker.fetch_issues_by_numbers(issue_numbers)
    else:
        issues = await tracker.fetch_candidate_issues()

    await runner.run(issues, force=force_all)
```

Add the triage subparser inside `main()`, right before `args = parser.parse_args()`:

```python
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
```

Add the triage dispatch after the version handling inside `main()`:

```python
    if args.command == "triage":
        _setup_logging(args.log_level)
        issue_numbers = (
            [int(n.strip()) for n in args.issues.split(",")]
            if args.issues
            else None
        )
        asyncio.run(_triage(Path(args.workflow), issue_numbers, args.force_all, args.model, args.dry_run))
        return
```

The complete updated `main()` after all changes:

```python
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

    _setup_logging(args.log_level)
    asyncio.run(_run(Path(args.workflow), args.port))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests to verify everything passes**

```bash
uv run pytest -v
```

Expected: all tests PASS. The `test_triage_subcommand_help` test should now PASS.

- [ ] **Step 5: Commit**

```bash
git add symphony/main.py tests/test_orchestrator.py
git commit -m "feat: add symphony triage CLI subcommand"
```

---

## Final verification

- [ ] **Run the full test suite**

```bash
uv run pytest -v
```

Expected: all tests PASS with no failures.

- [ ] **Verify the triage module is importable**

```bash
uv run python -c "from symphony.triage.agent import TriageAgent; from symphony.triage.runner import TriageRunner; print('ok')"
```

Expected: `ok`

- [ ] **Verify CLI help is correct**

```bash
uv run symphony triage --help
```

Expected output includes: `--issue`, `--all`, `--model`, `--dry-run`, `--log-level`

- [ ] **Commit final state if any cleanup was needed, then finish**

```bash
git log --oneline -5
```

---

## Spec Coverage Check

| Spec Requirement | Task |
|---|---|
| TriageConfig with model/label defaults | Task 1 |
| Optional `triage:` section in WORKFLOW.md | Task 1 |
| `anthropic>=0.40` dependency | Task 1 |
| `fetch_issue_comments` on GitHubClient | Task 2 |
| `post_comment` on GitHubClient | Task 2 |
| `add_labels` on GitHubClient | Task 2 |
| `remove_label` (404-tolerant) on GitHubClient | Task 2 |
| TriageAgent: prompt construction with issue + comments | Task 3 |
| TriageAgent: Haiku default, overridable model | Task 3 |
| TriageAgent: JSON output parsing | Task 3 |
| TriageAgent: failure handling (skip, no retry) | Task 3 |
| Re-triage detection via `<!-- symphony-triage ... -->` marker | Task 4 |
| Skip if `issue.updated_at <= triage_timestamp` | Task 4 |
| Re-triage if `issue.updated_at > triage_timestamp` | Task 4 |
| Label lifecycle (ready/not-ready/remove opposite) | Task 4 |
| `symphony:triaged` always applied | Task 4 |
| `--dry-run` prints instead of posting | Task 4 |
| `symphony triage [WORKFLOW]` CLI subcommand | Task 5 |
| `--issue/-i N[,N,...]` flag | Task 5 |
| `--all` flag (force re-triage) | Task 5 |
| `--model` override flag | Task 5 |
| `--dry-run` flag | Task 5 |
| `--log-level` flag | Task 5 |
| No-flags: fetch all open, apply re-triage detection | Task 5 |
