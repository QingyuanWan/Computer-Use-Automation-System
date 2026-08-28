"""Phase-3 escalation-semantics contracts at the REPLAY layer (items 1-7).

No browser / LLM: a fake executor drives the engine's error classification + escalation routing directly, so
each contract is fast and deterministic. The distinction under test (Phase-3 D1/D5/D6):

  - checkpoint_timeout / locator_exhaustion / safety_violation / find_matching_exhausted are STUCK conditions
    a human can resolve  -> escalate when a human is reachable (is_interactive handler);
  - an interactive Abort  -> hard_failure(human_aborted);
  - a non-interactive handler (the stub) -> hard_failure(stub_unavailable) with NO escalation attempt;
  - a raw technical exception from the executor -> hard_failure(technical_error:<detail>) with NO escalation.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.executor import ActionResult, CheckpointResult, ExecutorError
from src.models import Artifact, ArtifactMetadata, Checkpoint, ClickAction, Locator, SuccessCriteria
from src.replay import EscalationHandler, EscalationOutcome, ReplayEngine, StubEscalationHandler
from src.replay.results import SafetyViolationError


# ---------------- fakes ----------------

class _Handler(EscalationHandler):
    """Interactive handler that returns a scripted sequence of actions across successive escalate() calls
    (the last action repeats), recording the reason it was shown each time."""
    is_interactive = True

    def __init__(self, *actions, on_escalate=None):
        self._actions = list(actions) or ["abort"]
        self.calls: list[str] = []
        self._on = on_escalate

    async def escalate(self, ctx) -> EscalationOutcome:
        self.calls.append(ctx.reason)
        if self._on is not None:
            await self._on()
        i = min(len(self.calls) - 1, len(self._actions) - 1)
        return EscalationOutcome(action=self._actions[i], operator_note="note")


def _fake_exec(execute=None, checkpoint=None, url="http://x/"):
    return SimpleNamespace(
        escalation_handler=None,
        page=SimpleNamespace(url=url),
        execute_action=execute or AsyncMock(return_value=ActionResult(status="success", action="click")),
        resolve_checkpoint=checkpoint or AsyncMock(return_value=CheckpointResult(status="success")))


def _engine(ex, handler):
    return ReplayEngine(ex, handler, artifact_loader=lambda n: None)


def _meta(**kw):
    kw.setdefault("capability_type", "read")
    return ArtifactMetadata(capability_name="t", **kw)


def _art(*steps):
    return Artifact(version="0.1.0", metadata=_meta(), steps=list(steps))


def _click(id_="s1", css="#x", checkpoint=None):
    return ClickAction(id=id_, locator=Locator(strategy="css_id", css=css), checkpoint=checkpoint)


def _cp():
    return Checkpoint(success=SuccessCriteria(required_phrases=["OK"], target="#x"))


# ---------------- contract 1: checkpoint_timeout ----------------

async def test_contract1_checkpoint_timeout_escalates_then_abort():
    cp = AsyncMock(return_value=CheckpointResult(status="checkpoint_timeout", observed_text="loading",
                                                 screenshot_path="/e/s.png"))
    handler = _Handler("abort")
    res = await _engine(_fake_exec(checkpoint=cp), handler).replay(_art(_click(checkpoint=_cp())), {})
    assert handler.calls == ["checkpoint_timeout"]                # panel shown, correct reason
    assert res.status == "hard_failure" and res.reason == "human_aborted"
    assert res.operator_note == "note"


async def test_contract1_checkpoint_timeout_resume_resolves():
    cp = AsyncMock(side_effect=[CheckpointResult(status="checkpoint_timeout", observed_text="loading"),
                               CheckpointResult(status="success")])
    handler = _Handler("resume")
    res = await _engine(_fake_exec(checkpoint=cp), handler).replay(_art(_click(checkpoint=_cp())), {})
    assert res.status == "success" and handler.calls == ["checkpoint_timeout"]


# ---------------- contract 2: locator_exhaustion ----------------

async def test_contract2_locator_exhaustion_escalates():
    ex = _fake_exec(execute=AsyncMock(side_effect=ExecutorError("no match", screenshot_path="/e/s.png")))
    handler = _Handler("abort")
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert handler.calls == ["locator_exhausted"]
    assert res.status == "hard_failure" and res.reason == "human_aborted"


async def test_contract2_locator_resume_retry_succeeds():
    ex = _fake_exec(execute=AsyncMock(side_effect=[ExecutorError("no match"),
                                                   ActionResult(status="success", action="click")]))
    handler = _Handler("resume")
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert res.status == "success" and handler.calls == ["locator_exhausted"]


# ---------------- contract 3: safety_violation ----------------

async def test_contract3_safety_violation_escalates():
    ex = _fake_exec(execute=AsyncMock(side_effect=SafetyViolationError("blocked")))
    handler = _Handler("abort")
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert handler.calls == ["safety_violation"]
    assert res.status == "hard_failure" and res.reason == "human_aborted"


# ---------------- contract 4: find_matching_exhausted (returned status, not raised) ----------------

async def test_contract4_find_matching_exhausted_escalates():
    ex = _fake_exec(execute=AsyncMock(return_value=ActionResult(
        status="find_matching_exhausted", action="find_matching", screenshot_path="/e/s.png")))
    handler = _Handler("abort")
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert handler.calls == ["find_matching_exhausted"]
    assert res.status == "hard_failure" and res.reason == "human_aborted"


# ---------------- contract 5: non-interactive stub -> stub_unavailable, NO escalation ----------------

async def test_contract5_stub_action_failure_is_stub_unavailable():
    ex = _fake_exec(execute=AsyncMock(side_effect=ExecutorError("no match")))
    res = await _engine(ex, StubEscalationHandler()).replay(_art(_click()), {})
    assert res.status == "hard_failure" and res.reason == "stub_unavailable"


async def test_contract5_stub_checkpoint_timeout_is_stub_unavailable():
    cp = AsyncMock(return_value=CheckpointResult(status="checkpoint_timeout", observed_text="x"))
    res = await _engine(_fake_exec(checkpoint=cp), StubEscalationHandler()).replay(_art(_click(checkpoint=_cp())), {})
    assert res.status == "hard_failure" and res.reason == "stub_unavailable"


async def test_contract5_stub_returned_status_is_stub_unavailable():
    # a NON-raised failure status (find_matching_exhausted) with the stub -> stub_unavailable (review M4)
    ex = _fake_exec(execute=AsyncMock(return_value=ActionResult(status="find_matching_exhausted", action="find_matching")))
    res = await _engine(ex, StubEscalationHandler()).replay(_art(_click()), {})
    assert res.status == "hard_failure" and res.reason == "stub_unavailable"


# ---------------- contract 6: interactive Abort -> human_aborted (+ operator_note passthrough) ----------------

async def test_contract6_human_abort_carries_operator_note():
    ex = _fake_exec(execute=AsyncMock(side_effect=ExecutorError("no match")))
    handler = _Handler("abort")
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert res.status == "hard_failure" and res.reason == "human_aborted" and res.operator_note == "note"


# ---------------- contract 7: raw technical error -> technical_error, NO escalation ----------------

async def test_contract7_technical_error_no_escalation():
    ex = _fake_exec(execute=AsyncMock(side_effect=RuntimeError("playwright crashed")))
    handler = _Handler("resume")
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert handler.calls == []                                    # a human cannot fix a crash -> never escalated
    assert res.status == "hard_failure" and res.reason == "technical_error:playwright crashed"


# ---------------- extra: takeover_resume skips this step's binding and continues ----------------

async def test_takeover_resume_continues_to_next_step():
    ex = _fake_exec(execute=AsyncMock(side_effect=[ExecutorError("no match"),
                                                   ActionResult(status="success", action="click")]))
    handler = _Handler("takeover_resume")
    res = await _engine(ex, handler).replay(_art(_click("s1", "#a"), _click("s2", "#b")), {})
    assert res.status == "success" and handler.calls == ["locator_exhausted"]


# ---------------- extra: bounded escalation loop -> technical_error after _MAX_ESCALATION_ROUNDS ----------------

async def test_action_unresolved_after_bounded_rounds():
    ex = _fake_exec(execute=AsyncMock(side_effect=ExecutorError("no match")))
    handler = _Handler("resume")                                  # resume forever, but the action never recovers
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert res.status == "hard_failure"
    assert res.reason == "escalation_exhausted:action_unresolved_after_escalation"
    assert len(handler.calls) == 3                                # _MAX_ESCALATION_ROUNDS


async def test_checkpoint_unresolved_after_bounded_rounds():
    # checkpoint that never recovers under a resume-forever handler -> exhausted after the bound (review M2)
    cp = AsyncMock(return_value=CheckpointResult(status="checkpoint_timeout", observed_text="loading"))
    handler = _Handler("resume")
    res = await _engine(_fake_exec(checkpoint=cp), handler).replay(_art(_click(checkpoint=_cp())), {})
    assert res.status == "hard_failure"
    assert res.reason == "escalation_exhausted:checkpoint_unresolved_after_escalation"
    assert len(handler.calls) == 3


# ---------------- Bug-fix: 'exhausted' outcome + escalation_exhausted subtype ----------------

async def test_exhausted_outcome_maps_to_escalation_exhausted_action():
    # handler signals 'exhausted' (no human response / take-over timeout) -> escalation_exhausted, not abort
    ex = _fake_exec(execute=AsyncMock(side_effect=ExecutorError("no match")))
    handler = _Handler("exhausted")
    res = await _engine(ex, handler).replay(_art(_click()), {})
    assert res.status == "hard_failure"
    assert res.reason == "escalation_exhausted:no_response_or_takeover_timeout"
    assert len(handler.calls) == 1                                # settled on the first exhausted signal


async def test_exhausted_outcome_maps_to_escalation_exhausted_checkpoint():
    cp = AsyncMock(return_value=CheckpointResult(status="checkpoint_timeout", observed_text="x"))
    handler = _Handler("exhausted")
    res = await _engine(_fake_exec(checkpoint=cp), handler).replay(_art(_click(checkpoint=_cp())), {})
    assert res.status == "hard_failure"
    assert res.reason == "escalation_exhausted:no_response_or_takeover_timeout"
    assert len(handler.calls) == 1


async def test_repeated_takeover_cycles_exhaust_to_escalation_exhausted():
    # every take-over completes (Done) but the checkpoint keeps timing out -> after _MAX rounds -> exhausted
    cp = AsyncMock(return_value=CheckpointResult(status="checkpoint_timeout", observed_text="loading"))
    handler = _Handler("takeover_resume")
    res = await _engine(_fake_exec(checkpoint=cp), handler).replay(_art(_click(checkpoint=_cp())), {})
    assert res.status == "hard_failure"
    assert res.reason == "escalation_exhausted:checkpoint_unresolved_after_escalation"
    assert len(handler.calls) == 3                                # three take-over cycles, each re-evaluated once
