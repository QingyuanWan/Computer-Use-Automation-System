"""Enumerations used across the artifact schema.

- CapabilityType — read | write | mutating (ADR-005 Gap #0; schema-draft §2). Gates the failure-injection
  sub-run (only `read`).
- ActionType — the discovery-agent tool set (ADR-006; schema-draft §5). `finish` is a discovery-time tool,
  not a runtime step.
- EscalationReason / HumanOutcome — evidence-contract enums for takeover events (ADR-007; schema-draft §9).
"""
from __future__ import annotations

from enum import Enum


class CapabilityType(str, Enum):
    read = "read"
    write = "write"
    mutating = "mutating"


class ActionType(str, Enum):
    click = "click"
    type_text = "type_text"
    navigate = "navigate"
    read_text = "read_text"
    find_matching = "find_matching"
    human_input = "human_input"          # planned human intervention (ADR-007 planned mode)
    finish = "finish"


class EscalationReason(str, Enum):
    checkpoint_timeout = "checkpoint_timeout"
    locator_fail_after_n = "locator_fail_after_n"
    safety_guardrail = "safety_guardrail"
    llm_signal = "llm_signal"


class HumanOutcome(str, Enum):
    resume = "resume"
    takeover_resume = "takeover_resume"
    abort = "abort"
