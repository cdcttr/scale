"""The agent's final status line.

Agents end their final message with exactly one of:
    SCALE-STATUS: done
    SCALE-STATUS: blocked: <one-line reason>

This is how the orchestrator tells "blocked with a reason" (escalate to a human)
apart from "did nothing" (retry). Parsing is deterministic: the line must stand on
its own, and the last such line wins.
"""
from __future__ import annotations

import re
from typing import Optional

_STATUS_RE = re.compile(r"^\s*SCALE-STATUS:\s*(done|blocked)\s*(?::\s*(.*?))?\s*$", re.IGNORECASE)


def parse_agent_status(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ("done", None), ("blocked", reason) or (None, None) if no status line."""
    for line in reversed((text or "").splitlines()):
        m = _STATUS_RE.match(line)
        if m:
            status = m.group(1).lower()
            return (status, (m.group(2) or "")) if status == "blocked" else (status, None)
    return None, None
