"""Wiring tests: PlaywrightEscalationHandler is a drop-in for StubEscalationHandler in ReplayEngine
(constructor injection; no engine change)."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.escalation import PlaywrightEscalationHandler
from src.replay import ReplayEngine
from src.replay.escalation_seam import EscalationHandler, StubEscalationHandler


def test_is_valid_escalation_handler_subclass():
    assert issubclass(PlaywrightEscalationHandler, EscalationHandler)
    # satisfies the ABC (escalate implemented) — instantiable
    h = PlaywrightEscalationHandler(MagicMock(), evidence_dir=".", capability_name="cap")
    assert isinstance(h, EscalationHandler)


def test_injectable_into_replayengine_in_place_of_stub(tmp_path):
    page = MagicMock()
    handler = PlaywrightEscalationHandler(page, evidence_dir=tmp_path, capability_name="cap")
    engine = ReplayEngine(executor=MagicMock(), escalation_handler=handler,
                          artifact_loader=lambda name: None)
    assert engine.escalation_handler is handler                # same slot the stub occupied

    # the stub goes in the identical constructor position — no interface difference to ReplayEngine
    stub_engine = ReplayEngine(executor=MagicMock(), escalation_handler=StubEscalationHandler(),
                               artifact_loader=lambda name: None)
    assert type(engine.escalation_handler).escalate is not type(stub_engine.escalation_handler).escalate


def test_exposes_takeover_flag_for_recorder():
    # the recorder-integration contract Phase A only exposes (wiring deferred): a readable boolean property
    h = PlaywrightEscalationHandler(MagicMock(), evidence_dir=".")
    assert hasattr(h, "is_takeover_active") and h.is_takeover_active is False
