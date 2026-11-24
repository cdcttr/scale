from __future__ import annotations

from scale.config.schema import TrackerConfig
from scale.tracker.base import TrackerClient


def make_tracker(config: TrackerConfig) -> TrackerClient:
    """Build the work tracker named by tracker.kind."""
    if config.kind == "beads":
        from scale.tracker.beads import BeadsTracker
        return BeadsTracker(config)
    from scale.tracker.github import GitHubClient
    return GitHubClient(config)
