"""Safety-layer error.

Raised by the safety module (checkers / `SafetyGate`) when a guardrail blocks an action or a capability. It
carries `rule_name` so the replay engine can surface `hard_failure(reason="safety_blocked:<rule_name>")`. This
is intentionally distinct from the discovery-time `src/replay/results.py::SafetyViolationError` escalation seam:
a safety BLOCK here is terminal (no human override), not an escalatable stuck condition.
"""
from __future__ import annotations


class SafetyViolationError(Exception):
    """A safety guardrail blocked an action/capability. `rule_name` names the specific rule (e.g.
    'allowlist_domain', 'allowlist_action_type', 'capability_type_mismatch')."""

    def __init__(self, rule_name: str, detail: str = "") -> None:
        self.rule_name = rule_name
        self.detail = detail
        super().__init__(f"safety_blocked:{rule_name}" + (f" ({detail})" if detail else ""))
