"""The composition root (S10): the JobRegistry the Hub serves with.

`campuslife-hub serve` runs default_registry(): capture.ingest, the file pipeline's identify, every extractor in
extractors.ALL, and its planner. A test builds a registry with other extractors (extract.fakes) the same way.
"""

from __future__ import annotations

from collections.abc import Sequence

from .extract import extractors
from .extract.extractors import Extractor
from .extract.framework import Files
from .extract.limits import DEFAULT_LIMITS, Limits
from .job_runner import JobRegistry


def build_registry(extractor_list: Sequence[Extractor] = extractors.ALL, limits: Limits = DEFAULT_LIMITS) -> JobRegistry:
    registry = JobRegistry()
    Files(extractor_list, limits).register(registry)
    return registry


def default_registry() -> JobRegistry:
    return build_registry()
