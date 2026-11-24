from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class TrackerConfig(BaseModel):
    kind: Literal["github"] = "github"
    repo: str
    api_token: str
    default_branch: str = "main"
    active_labels: list[str] = []
    skip_labels: list[str] = ["scale:skip"]
    terminal_labels: list[str] = ["scale:done"]


class PollingConfig(BaseModel):
    interval_ms: int = 30000


class WorkspaceConfig(BaseModel):
    root: str = "./workspaces"
    log_archive: Optional[str] = None


class HooksConfig(BaseModel):
    after_create: str = ""
    before_run: str = ""
    after_run: str = ""
    before_remove: str = ""
    timeout_ms: int = 60000


class AgentConfig(BaseModel):
    max_concurrent_agents: int = 10
    max_turns: int = 20
    max_retry_backoff_ms: int = 300000
    max_concurrent_agents_by_state: dict[str, int] = {}
    completed_display_s: int = 300
    supervised_label: str = "scale:supervised"
    auto_merge: bool = False
    # "agent": the agent pushes and opens the PR itself (needs credentials in its workspace).
    # "orchestrator": the agent only commits; Scale pushes and opens the PR (scale/publish.py).
    publish: Literal["agent", "orchestrator"] = "agent"


class CodexConfig(BaseModel):
    command: str = "claude"
    approval_policy: Literal["auto"] = "auto"
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000
    stall_grace_period_ms: int = 300_000
    stall_heartbeat_s: float = 60.0


class ServerConfig(BaseModel):
    port: int
    api_token: str


class SandboxConfig(BaseModel):
    """Run each agent in a disposable container in a Colima VM (scale/worker/colima.py).

    The box gets only its workspace and a receipts folder; no credentials. Model
    calls go through agent-gate, which holds the real credential.
    """
    kind: Literal["colima"] = "colima"
    docker_context: str = "colima-agents"
    image: str = "agent-box"
    network: str = "agents-internal"          # --internal network; agent-gate is the only way out
    share_root: str = "~/agent-work"          # the only host folder the VM can see
    agent_command: str = "claude --dangerously-skip-permissions"
    box_env: dict[str, str] = {"ANTHROPIC_AUTH_TOKEN": "dummy-not-a-secret"}
    cpus: str = "2"
    memory: str = "4g"
    pids_limit: int = 512
    start_timeout_s: float = 60.0
    keep_box: bool = False                    # leave the box running after the turn (for inspection)


class WorkerConfig(BaseModel):
    ssh_hosts: list[str] = []
    max_concurrent_agents_per_host: int = 3
    sandbox: Optional[SandboxConfig] = None


class TriageConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    triage_label: str = "scale:triage"
    ready_label: str = "scale:ready"
    needs_detail_label: str = "scale:needs-detail"
    needs_approval_label: str = "scale:needs-approval"
    triaged_label: str = "scale:triaged"


class PlannerConfig(BaseModel):
    model: str = "claude-sonnet-4-6"
    max_depth: int = 3
    plan_label: str = "scale:plan"
    leaf_label: str = "scale:leaf"
    concept_label: str = "scale:concept"
    planned_label: str = "scale:planned"
    planner_workspace: str = "./workspaces/_planner"


class ReviewConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    timeout_ms: int = 120000
    pr_open_label: str = "scale:pr-open"
    needs_revision_label: str = "scale:needs-revision"
    no_verdict_label: str = "scale:needs-approval"
    conflict_label: str = "scale:conflict"
    template: str = ""
    merge_label: str = "scale:merge"
    feedback_enabled: bool = False


class RebaseConfig(BaseModel):
    model: str = "claude-sonnet-4-6"
    timeout_ms: int = 300_000
    conflict_label: str = "scale:conflict"
    template: str = ""


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
    planner: Optional[PlannerConfig] = None
    review: Optional[ReviewConfig] = None
    rebase: Optional[RebaseConfig] = None
    prompt_template: str = ""
