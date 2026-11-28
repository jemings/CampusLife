"""The composition root (S10): the JobRegistry the Hub serves with.

`campuslife-hub serve` runs default_registry(): capture.ingest, the file pipeline's identify, every extractor in
extractors.ALL, and its planner. A test builds a registry with other extractors (extract.fakes) the same way.
"""

from __future__ import annotations

from collections.abc import Sequence

from .extract import extractors
from .extract.extractors import Extractor
from .extract.framework import Files
from .extract.limits import IDENTIFY_LIMITS, Limits
from .job_runner import JobRegistry


def build_registry(extractor_list: Sequence[Extractor] = extractors.ALL, identify_limits: Limits = IDENTIFY_LIMITS) -> JobRegistry:
    """Each extractor runs under its descriptor's limits, identify under identify_limits."""
    registry = JobRegistry()
    Files(extractor_list, identify_limits).register(registry)
    return registry


def default_registry() -> JobRegistry:
    return build_registry()
