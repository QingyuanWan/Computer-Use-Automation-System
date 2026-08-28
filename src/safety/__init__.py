"""Safety module (ADR-008 §Safety) — runtime allowlist + input validation + PII redaction.

Public API: `SafetyGate` (compose + hooks), `SafetyPolicy` + `PARABANK_POLICY` (the injectable allow-set),
`PIIRedactor` (evidence redaction), `SafetyViolationError` (carries `rule_name`). The module imports nothing
from other `src/` packages — it is duck-typed against the action/artifact models — so it stays boundary-clean
and independently testable.
"""
from __future__ import annotations

from .errors import SafetyViolationError
from .parabank_allowlist import PARABANK_POLICY
from .policy import SafetyPolicy
from .policy_loader import load_policy
from .redactor import PIIRedactor
from .safety_gate import SafetyGate

__all__ = ["SafetyGate", "SafetyPolicy", "PARABANK_POLICY", "load_policy", "PIIRedactor",
           "SafetyViolationError"]
