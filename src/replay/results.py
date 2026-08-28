"""Typed replay results + replay-layer errors.

`ReplayResult` is the three-way outcome contract (ADR-005): success / business_outcome / hard_failure.
Built via classmethods so each variant only carries its relevant fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ReplayResult:
    status: str                                   # "success" | "business_outcome" | "hard_failure"
    outputs: dict[str, Any] = field(default_factory=dict)          # success: exported captures/params
    outcome_name: Optional[str] = None            # business_outcome: matched expected_outcome name
    observed_text: Optional[str] = None
    partial_captures: dict[str, Any] = field(default_factory=dict)  # business_outcome: exports so far
    failed_step_id: Optional[str] = None          # hard_failure
    expected: Optional[str] = None
    screenshot_path: Optional[str] = None
    reason: Optional[str] = None
    operator_note: Optional[str] = None

    @classmethod
    def success(cls, outputs: dict[str, Any]) -> "ReplayResult":
        return cls(status="success", outputs=dict(outputs))

    @classmethod
    def business_outcome(cls, name: str, observed_text: Optional[str] = None,
                         partial_captures: Optional[dict[str, Any]] = None) -> "ReplayResult":
        return cls(status="business_outcome", outcome_name=name, observed_text=observed_text,
                   partial_captures=dict(partial_captures or {}))

    @classmethod
    def hard_failure(cls, step_id: Optional[str] = None, expected: Optional[str] = None,
                     observed_text: Optional[str] = None, screenshot_path: Optional[str] = None,
                     reason: Optional[str] = None, operator_note: Optional[str] = None) -> "ReplayResult":
        return cls(status="hard_failure", failed_step_id=step_id, expected=expected,
                   observed_text=observed_text, screenshot_path=screenshot_path, reason=reason,
                   operator_note=operator_note)

    # Phase-3 D6: hard_failure is reserved for a fixed set of named subtypes, carried in `reason`:
    # stub_unavailable | human_aborted | escalation_exhausted | technical_error:<detail>.
    @classmethod
    def stub_unavailable(cls, step_id=None, expected=None, observed_text=None,
                         screenshot_path=None) -> "ReplayResult":
        """No human reachable (StubEscalationHandler / unattended)."""
        return cls.hard_failure(step_id=step_id, expected=expected, observed_text=observed_text,
                                screenshot_path=screenshot_path, reason="stub_unavailable")

    @classmethod
    def escalation_exhausted(cls, detail=None, step_id=None, expected=None, observed_text=None,
                             screenshot_path=None, operator_note=None) -> "ReplayResult":
        """The system escalated to a human and STILL could not resolve the stuck condition — the bounded
        escalation loop ran out of rounds, or the human's take-over/response window timed out. Distinct from
        human_aborted (the human did not choose Abort) and from technical_error (nothing crashed): this is a
        semantic exhaustion of the escalation options."""
        reason = f"escalation_exhausted:{detail}" if detail else "escalation_exhausted"
        return cls.hard_failure(step_id=step_id, expected=expected, observed_text=observed_text,
                                screenshot_path=screenshot_path, reason=reason, operator_note=operator_note)

    @classmethod
    def human_aborted(cls, step_id=None, expected=None, observed_text=None, screenshot_path=None,
                      operator_note=None) -> "ReplayResult":
        """The human explicitly chose Abort in the panel."""
        return cls.hard_failure(step_id=step_id, expected=expected, observed_text=observed_text,
                                screenshot_path=screenshot_path, reason="human_aborted",
                                operator_note=operator_note)

    @classmethod
    def technical_error(cls, detail: str, step_id=None, expected=None, observed_text=None,
                        screenshot_path=None) -> "ReplayResult":
        """A technical failure a human cannot resolve (Playwright crash, network, capture-binding, etc.)."""
        return cls.hard_failure(step_id=step_id, expected=expected, observed_text=observed_text,
                                screenshot_path=screenshot_path, reason=f"technical_error:{detail}")


class ReplayError(Exception):
    """Base replay-layer error (distinct from executor errors)."""


class CaptureBindingError(ReplayError):
    """A declared capture could not be produced/bound during replay (surfaces as hard_failure)."""


class SafetyViolationError(ReplayError):
    """A safety guardrail blocked an action during replay (Phase-3 D1). Routed through escalation (a human may
    override or abort). The safety module that raises this is not built yet; the type + routing exist so the
    seam is testable now."""
