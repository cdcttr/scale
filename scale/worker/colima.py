"""ColimaWorker: run the agent interactively in tmux inside a sandbox box.

Each run gets a disposable container in the Colima VM:
  - mounts only the issue's workspace (/work) and a per-attempt receipts folder
    (/receipts, outside the repo so it can't be committed by accident);
  - joins an --internal network whose only exit is agent-gate (model calls with
    the real credential injected there; everything else allowlisted or refused);
  - carries no credentials: a dummy model token, no git or tracker token.
    Publishing (push + PR) is done by the orchestrator (agent.publish: orchestrator).

The agent runs in tmux session "main", so an operator can watch
(`docker exec -it <box> tmux attach -r -t main`) or steer (drop -r).
In-box Claude Code hooks append events to /receipts/events.jsonl; the worker
pastes the prompt (bracketed paste, so newlines don't submit early), records it
in /receipts/sent.jsonl (prompts not listed there were typed by a human), and
waits for the Stop hook.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from scale.config.schema import WorkflowConfig
from scale.prompt.renderer import render_prompt
from scale.publish import apply_publish_mode
from scale.tracker.models import Issue
from scale.worker.base import Worker
from scale.workspace.manager import WorkspaceManager, sanitize_identifier

logger = logging.getLogger(__name__)

DockerFn = Callable[..., Awaitable[tuple[int, str, str]]]
_POLL_S = 0.5


def _box_name(identifier: str) -> str:
    return "scale-" + re.sub(r"[^a-z0-9_.-]", "-", sanitize_identifier(identifier).lower())


def make_docker(context: str) -> DockerFn:
    async def docker(*args: str, input: Optional[str] = None) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            env={**os.environ, "DOCKER_CONTEXT": context},
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(input.encode() if input is not None else None)
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
    return docker


class ColimaWorker(Worker):
    def __init__(self, workspace: WorkspaceManager, config: WorkflowConfig,
                 docker: Optional[DockerFn] = None) -> None:
        sb = config.worker.sandbox
        if sb is None:
            raise ValueError("ColimaWorker needs worker.sandbox in the config")
        if config.agent.publish != "orchestrator":
            raise ValueError("worker.sandbox requires agent.publish: orchestrator (the box has no credentials to push with)")
        share = Path(sb.share_root).expanduser().resolve()
        root = Path(config.workspace.root).expanduser().resolve()
        if not root.is_relative_to(share):
            raise ValueError(f"workspace.root {root} must be inside sandbox share_root {share} (the only folder the VM can see)")
        self._workspace = workspace
        self._config = config
        self._sb = sb
        self._root = root
        self._docker = docker or make_docker(sb.docker_context)

    async def _d(self, *args: str, input: Optional[str] = None, check: bool = True) -> str:
        rc, out, err = await self._docker(*args, input=input)
        if check and rc != 0:
            raise RuntimeError(f"docker {args[0]} failed (exit {rc}): {err.strip()[:300]}")
        return out

    async def _alive(self, box: str) -> bool:
        rc, out, _ = await self._docker("inspect", "-f", "{{.State.Running}}", box)
        return rc == 0 and out.strip() == "true"

    @staticmethod
    def _read_events(path: Path, offset: int) -> tuple[list[dict], int]:
        if not path.exists():
            return [], offset
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
        complete = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
        events = []
        for line in complete.splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events, offset + len(complete)

    async def run(
        self,
        issue: Issue,
        config: WorkflowConfig,
        attempt: Optional[int],
        on_event: Optional[Callable[[dict], None]] = None,
        previous_attempt_summary: Optional[str] = None,
    ) -> None:
        emit = on_event or (lambda e: None)
        workspace_path = await self._workspace.prepare(issue)
        await self._workspace.run_before_hook(issue)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        receipts = self._root / "_receipts" / sanitize_identifier(issue.identifier) / f"{stamp}-a{attempt or 1}"
        receipts.mkdir(parents=True, exist_ok=True)
        events_path = receipts / "events.jsonl"
        box = _box_name(issue.identifier)
        sb = self._sb

        await self._d("rm", "-f", box, check=False)
        env_args = [x for k, v in sb.box_env.items() for x in ("-e", f"{k}={v}")]
        try:
            await self._d(
                "run", "-d", "--name", box, "--hostname", box[:63],
                "--network", sb.network,
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", str(sb.pids_limit), "--cpus", sb.cpus, "--memory", sb.memory,
                "--label", f"scale.issue={issue.identifier}",
                *env_args,
                "-v", f"{workspace_path}:/work",
                "-v", f"{receipts}:/receipts",
                sb.image,
            )
            await self._d("exec", box, "tmux", "new-session", "-d", "-s", "main",
                          "-x", "220", "-y", "50", sb.agent_command)

            offset = 0
            deadline = time.monotonic() + sb.start_timeout_s
            started = False
            while not started:
                evs, offset = self._read_events(events_path, offset)
                for ev in evs:
                    emit({"type": "scale:hook", "event": ev.get("hook_event_name"), "data": ev})
                    started = started or ev.get("hook_event_name") == "SessionStart"
                if started:
                    break
                if time.monotonic() > deadline or not await self._alive(box):
                    raise RuntimeError(f"agent session in {box} did not start within {sb.start_timeout_s:.0f}s")
                await asyncio.sleep(_POLL_S)

            prompt = apply_publish_mode(
                render_prompt(config.prompt_template, issue, attempt, previous_attempt_summary), config
            )
            with open(receipts / "sent.jsonl", "a") as f:
                f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                    "sender": "orchestrator", "prompt": prompt}) + "\n")
            await self._d("exec", "-i", box, "tmux", "load-buffer", "-b", "scale", "-", input=prompt)
            await self._d("exec", box, "tmux", "paste-buffer", "-p", "-d", "-b", "scale", "-t", "main")
            await asyncio.sleep(0.5)
            await self._d("exec", box, "tmux", "send-keys", "-t", "main", "Enter")
            logger.info("issue_identifier=%s prompt sent to %s (receipts %s)", issue.identifier, box, receipts)

            deadline = time.monotonic() + config.codex.turn_timeout_ms / 1000
            stop: Optional[dict] = None
            while stop is None:
                evs, offset = self._read_events(events_path, offset)
                for ev in evs:
                    emit({"type": "scale:hook", "event": ev.get("hook_event_name"), "data": ev})
                    if ev.get("hook_event_name") == "Stop":
                        stop = ev
                if stop is not None:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError(f"agent turn in {box} timed out after {config.codex.turn_timeout_ms / 1000:.0f}s")
                if not await self._alive(box):
                    raise RuntimeError(f"agent box {box} exited before the turn finished")
                await asyncio.sleep(_POLL_S)

            emit({"type": "scale:final", "message": stop.get("last_assistant_message") or ""})
            for t in receipts.glob("transcript-*.jsonl"):
                for line in t.read_text(errors="replace").splitlines():
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    usage = (entry.get("message") or {}).get("usage") if entry.get("type") == "assistant" else None
                    if usage:
                        emit({"type": "assistant", "message": {"usage": usage}})
            logger.info("issue_identifier=%s turn finished: %s", issue.identifier,
                        (stop.get("last_assistant_message") or "")[:200])
        finally:
            if not sb.keep_box:
                await self._d("rm", "-f", box, check=False)
            await self._workspace.run_after_hook(issue)
