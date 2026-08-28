"""Replay engine (ADR-005/ADR-006/ADR-007). Public surface for the demo CLI and tests.

Boundary (ADR-008): depends only on src/models + src/executor. Never imports agent/storage/safety or the
real src/escalation/ UI; the takeover UI is reached only through the injected EscalationHandler seam.
"""
from __future__ import annotations

from .engine import ReplayEngine
from .escalation_seam import (
    EscalationContext,
    EscalationHandler,
    EscalationOutcome,
    StubEscalationHandler,
)
from .results import (
    CaptureBindingError,
    ReplayError,
    ReplayResult,
)
from .scope import VariableScope, new_scope

__all__ = [
    "ReplayEngine",
    "ReplayResult",
    "EscalationHandler",
    "StubEscalationHandler",
    "EscalationContext",
    "EscalationOutcome",
    "VariableScope",
    "new_scope",
    "ReplayError",
    "CaptureBindingError",
]
