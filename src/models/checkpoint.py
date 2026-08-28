"""Checkpoint type (ADR-005; schema-draft §7).

`text_all_present`: every phrase in `success.required_phrases` must appear (AND) as a substring of the
`target` region's text. `expected_outcomes` are named business-outcome branches (populated only for `read`
capabilities via the failure-injection sub-run — schema-draft §8). Polling is async-tolerant: `wait_ms`
(default 5000, heuristic — ADR-005) with cadence `poll_interval_ms` (default 500). `target` defaults to
`#rightPanel`, a ParaBank config default (overridable), not a schema constant.
"""
from __future__ import annotations

from pydantic import Field, model_validator

from .base import StrictModel


class SuccessCriteria(StrictModel):
    required_phrases: list[str]
    target: str = "#rightPanel"


class ExpectedOutcome(StrictModel):
    name: str
    required_phrases: list[str]


class Checkpoint(StrictModel):
    success: SuccessCriteria
    expected_outcomes: list[ExpectedOutcome] = Field(default_factory=list)
    wait_ms: int = Field(default=5000, gt=0)
    poll_interval_ms: int = Field(default=500, gt=0)

    @model_validator(mode="after")
    def _wait_ge_poll(self) -> "Checkpoint":
        if self.wait_ms < self.poll_interval_ms:
            raise ValueError(
                f"wait_ms ({self.wait_ms}) must be >= poll_interval_ms ({self.poll_interval_ms})"
            )
        return self
