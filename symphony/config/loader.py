from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any

import frontmatter
from symphony.config.schema import WorkflowConfig

_VAR_RE = re.compile(r'^\$([A-Z_][A-Z0-9_]*)$')


def resolve_vars(obj: Any) -> Any:
    if isinstance(obj, str):
        m = _VAR_RE.match(obj)
        if m:
            name = m.group(1)
            val = os.environ.get(name)
            if val is None:
                raise ValueError(f"Environment variable ${name} is not set")
            return val
        return obj
    if isinstance(obj, dict):
        return {k: resolve_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_vars(v) for v in obj]
    return obj


def load_workflow(path: Path) -> WorkflowConfig:
    post = frontmatter.load(str(path))
    data: dict = dict(post.metadata)
    data = resolve_vars(data)
    data["prompt_template"] = post.content

    ws = data.get("workspace", {})
    root = ws.get("root", "./workspaces")
    if not os.path.isabs(root):
        root = root.replace("~", os.path.expanduser("~"))
        root = str((path.parent / root).resolve())
    if "workspace" not in data:
        data["workspace"] = {}
    data["workspace"]["root"] = root

    return WorkflowConfig.model_validate(data)
