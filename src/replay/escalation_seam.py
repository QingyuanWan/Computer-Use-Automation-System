"""Escalation seam (ADR-007).

ReplayEngine calls into an EscalationHandler when a checkpoint times out or an action fails. This module
defines ONLY the abstract interface + a stub. The real overlay-injecting handler lives in src/escalation/
(a later module) and is substituted by constructor injection — replay never imports it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class EscalationContext:
    step_id: str
    reason: str                       # "checkpoint_timeout" | "locator_exhausted" | "find_matching_exhausted" ...
    current_url: str
    observed_text: str
    screenshot_path: Optional[str] = None
    hint: Optional[str] = None        # ADR-007 revision (D3-α): operator hint shown in the takeover panel


@dataclass(frozen=True)
class EscalationOutcome:
    # resume / takeover_resume / abort come from the human's button. "exhausted" is a SYSTEM signal (no button):
    # the escalation wait timed out with no human response (reactive three-button OR the take-over Done wait),
    # so the system has run out of options -> the engine maps it to hard_failure(escalation_exhausted), NOT
    # human_aborted (the human did not choose to abort) and NOT technical_error (nothing crashed).
    action: Literal["resume", "takeover_resume", "abort", "exhausted"]
    operator_note: Optional[str] = None    # per ADR-007: human-authored "what did you do?"


class EscalationHandler(ABC):
    # True for a handler that can actually reach a human (the overlay). False for non-interactive handlers
    # (the stub, unattended CI) — the replay/discovery layers use this to choose stub_unavailable vs an
    # interactive escalation (ADR-007 / Phase-3 D6).
    is_interactive: bool = True

    @abstractmethod
    async def escalate(self, context: EscalationContext) -> EscalationOutcome:
        """Reactive escalation (stuck): show the panel, block until the operator resolves."""
        ...

    async def escalate_planned(self, prompt: str, reason: str, timeout_ms: int = 60000,
                               hint: Optional[str] = None) -> None:
        """Planned intervention (ADR-007 planned mode): show the panel in planned mode (prompt + single Done),
        block until the human clicks Done, then return (void — the human acts on the page, no value returns).
        `hint` (ADR-007 revision, D3-α) is the optional operator hint shown in the panel. Raises
        asyncio.TimeoutError on timeout. Default impl is a no-op so non-interactive handlers (the stub, the
        unattended validation gate) don't block; the real overlay handler overrides it."""
        return None


class StubEscalationHandler(EscalationHandler):
    """Placeholder: NO real UI — reactive escalation always aborts; planned intervention is a no-op (returns
    immediately, as an unattended context has no human). Replaced by the src/escalation/ overlay handler."""

    is_interactive = False   # no human reachable -> callers map to hard_failure(stub_unavailable)

    async def escalate(self, context: EscalationContext) -> EscalationOutcome:
        return EscalationOutcome(action="abort", operator_note=None)

    async def escalate_planned(self, prompt: str, reason: str, timeout_ms: int = 60000,
                               hint: Optional[str] = None) -> None:
        return None
