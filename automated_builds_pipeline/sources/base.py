"""Shared source fetcher result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from automated_builds_pipeline.stats import WindowObservation

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
SKIPPED = "skipped"
VALID_HEALTH_STATUSES = frozenset({HEALTHY, UNHEALTHY, SKIPPED})


@dataclass
class FetchOptions:
    observed_at: Optional[str] = None
    artifact_ref: Optional[str] = None
    timeout_seconds: int = 30
    expected_patch_label: Optional[str] = None
    source_url: Optional[str] = None
    article_slugs: list[str] = field(default_factory=list)
    days: int = 30
    since: Optional[str] = None
    catalog_dir: Optional[str] = None
    names_file: Optional[str] = None


@dataclass
class SourceFetchResult:
    observation: WindowObservation
    status: str
    details: list[str] = field(default_factory=list)
    patch_label: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in VALID_HEALTH_STATUSES:
            raise ValueError(f"invalid source health status: {self.status}")
        self.observation.health_status = self.status
        self.observation.details = list(self.details)

