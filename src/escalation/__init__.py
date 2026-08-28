"""In-browser human-takeover escalation (ADR-007). PlaywrightEscalationHandler is a drop-in replacement for
StubEscalationHandler, injected into ReplayEngine via constructor.

Boundary (ADR-008): depends only on src/replay/escalation_seam (the ABC) + stdlib. Never imports
agent/executor/storage/safety.
"""
from __future__ import annotations

from src.replay.escalation_seam import EscalationContext, EscalationOutcome

from .bridge import ResumeBridge
from .dom_diff import DOM_SNAPSHOT_JS, summarize
from .evidence_writer import build_event, write_escalation_event
from .handler import PlaywrightEscalationHandler
from .panel_script import PANEL_JS_SOURCE

__all__ = [
    "PlaywrightEscalationHandler",
    "EscalationOutcome",
    "EscalationContext",
    "ResumeBridge",
    "PANEL_JS_SOURCE",
    "DOM_SNAPSHOT_JS",
    "summarize",
    "build_event",
    "write_escalation_event",
]
