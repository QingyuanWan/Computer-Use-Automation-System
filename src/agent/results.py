"""Typed results + recorded-step container + agent errors."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.models import Artifact


@dataclass
class RecordedStep:
    """One executed discovery action. `observation_after` is the step's *observation window* — the
    concatenation of every ARIA snapshot observed after this step executes and before the next recorded
    step (or the finish observation). A window (not a single snapshot) is required so that values which
    render asynchronously a turn or two later, or appear only in the finish-turn / post-request_screenshot
    observations, are still traced to a step (see the Phase-1 investigation)."""
    step_id: str
    action: Any                       # a models Step action (ClickAction / TypeTextAction / ...)
    turn: int = 0                     # discovery turn this action executed on (for window assignment)
    observation_after: str = ""       # concatenated observation window (set post-loop)
    action_status: str = "success"
    read_text_value: Optional[str] = None   # for read_text steps: the text returned (capture binding)


@dataclass
class DiscoveryResult:
    status: str                       # "success" | "max_steps" | "stuck" | "no_tool_call" | "error"
    artifact: Optional[Artifact] = None
    capability_name: str = ""
    evidence_dir: Optional[str] = None
    steps: int = 0
    dropped_exports: list[str] = field(default_factory=list)   # untraceable finish fields (ADR-006 Gap #4b)
    warnings: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)        # aggregate token metrics (cumulative in Part 2)
    wall_s: float = 0.0
    detail: Optional[str] = None
    # ---- Part 2: failure-injection + validation gate ----
    total_billed_tokens: int = 0                # cumulative billed tokens across happy + injection sub-runs
    injection_ran: bool = False                 # failure-injection sub-run executed (read capabilities only)
    injection_skip_reason: Optional[str] = None # why injection was skipped (write/mutating guard)
    expected_outcomes_added: int = 0            # number of expected_outcomes recorded by injection
    validation_status: Optional[str] = None     # replay status from the validation gate
    validation_detail: Optional[str] = None


class AgentError(Exception):
    pass


class FinishValidationError(AgentError):
    """finish() was called without the required success_observed_phrases (ADR-005)."""


class TranslationError(AgentError):
    """An LLM tool call could not be translated into a valid models action."""
