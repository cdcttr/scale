from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any

import frontmatter
from scale.config.schema import WorkflowConfig

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

    review_path = path.parent / "REVIEW.md"
    if review_path.exists():
        review_post = frontmatter.load(str(review_path))
        review_meta = resolve_vars(dict(review_post.metadata))
        review_section = review_meta.get("review") or {}
        review_section["template"] = review_post.content
        data["review"] = review_section

    rebase_path = path.parent / "REBASE.md"
    if rebase_path.exists():
        rebase_post = frontmatter.load(str(rebase_path))
        rebase_meta = resolve_vars(dict(rebase_post.metadata))
        rebase_section = rebase_meta.get("rebase") or {}
        rebase_section["template"] = rebase_post.content
        data["rebase"] = rebase_section

    return WorkflowConfig.model_validate(data)
