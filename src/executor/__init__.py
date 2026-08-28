"""Playwright executor backend (ADR-002 seam). Public surface for src/replay/ and tests.

Boundary (ADR-008): this package depends only on src/models + Playwright. It never imports agent/replay/
escalation/storage/safety and never calls an LLM.
"""
from __future__ import annotations

from .evidence import EvidenceCapture
from .executor import PlaywrightExecutor
from .results import (
    ActionResult,
    CheckpointResult,
    ExecutorError,
    ExecutorResult,
    InterpolationError,
    LocatorAmbiguityError,
    LocatorResolutionError,
    VariableScope,
)

__all__ = [
    "PlaywrightExecutor",
    "ExecutorResult",
    "ActionResult",
    "CheckpointResult",
    "VariableScope",
    "EvidenceCapture",
    "ExecutorError",
    "LocatorResolutionError",
    "LocatorAmbiguityError",
    "InterpolationError",
]
