from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Issue:
    id: str
    identifier: str
    number: int
    title: str
    description: str
    state: str
    labels: list[str]
    branch_name: str
    url: str
    priority: Optional[int]
    created_at: datetime
    updated_at: datetime
