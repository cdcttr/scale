from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class TrackerConfig(BaseModel):
    kind: Literal["github"] = "github"
    repo: str
    api_token: str
    active_labels: list[str] = []
    skip_labels: list[str] = ["symphony:skip"]
    terminal_labels: list[str] = ["symphony:done"]


class PollingConfig(BaseModel):
    interval_ms: int = 30000


class WorkspaceConfig(BaseModel):
    root: str = "./workspaces"


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


class CodexConfig(BaseModel):
    command: str = "claude"
    approval_policy: Literal["auto"] = "auto"
    turn_timeout_ms: int = 3_600_000
    read_timeout_ms: int = 5_000
    stall_timeout_ms: int = 300_000


class ServerConfig(BaseModel):
    port: int


class WorkerConfig(BaseModel):
    ssh_hosts: list[str] = []
    max_concurrent_agents_per_host: int = 3


class WorkflowConfig(BaseModel):
    tracker: TrackerConfig
    polling: PollingConfig = Field(default_factory=PollingConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    server: Optional[ServerConfig] = None
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    prompt_template: str = ""
