"""Human-takeover evidence record (ADR-007 §Consequences; schema-draft §9).

NOT part of the artifact YAML — this is the evidence-log contract for a runtime takeover event.
`dom_diff_summary` is a coarse structural summary (counts by role), never verbatim content;
`operator_note` is the optional free-text "what did you do?" the operator fills on resume.
"""
from __future__ import annotations

from typing import Optional

from .base import StrictModel
from .enums import EscalationReason, HumanOutcome


class EscalationEvent(StrictModel):
    escalation_at_step: str
    reason: EscalationReason
    timestamp: str
    human_outcome: HumanOutcome
    duration_s: float
    dom_diff_summary: str
    operator_note: Optional[str] = None
