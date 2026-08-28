"""Phase-2 Group-B unit tests: planned human intervention (ADR-007). All mocked — no browser, no LLM.

Covers contract items 1,2,3,4,5,6,8,9,10 (panel planned-mode UI is in tests/escalation/test_planned_mode.py;
CLI wiring in tests/cli/test_cli_unit.py)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.agent.agent as agent_mod
from src.agent import DiscoveryAgent
from src.agent.emission import apply_human_input_override, emit_artifact, reverse_parameterize_action
from src.agent.results import DiscoveryResult, RecordedStep
from src.agent.tools import TOOLS
from src.executor import action_dispatcher
from src.executor.results import ActionResult, ExecutorError
from src.models import (
    Artifact,
    ArtifactMetadata,
    CapabilityType,
    HumanInputAction,
    NavigateAction,
)
from src.replay import ReplayEngine, StubEscalationHandler


# ---- contract 1: tool available; description states return void ------------------------------------
def test_request_human_input_tool_present_and_void():
    tool = next((t for t in TOOLS if t["name"] == "request_human_input"), None)
    assert tool is not None
    desc = tool["description"].lower()
    assert "returns nothing" in desc or "return nothing" in desc or "nothing" in desc
    assert "observe" in desc                         # instructs re-observe after
    props = tool["input_schema"]["properties"]
    assert "prompt" in props and "reason" in props


# ---- contract 2: LLM request_human_input during discovery → escalate_planned + records the step -----
async def test_agent_handles_request_human_input(tmp_path):
    handler = SimpleNamespace(escalate_planned=AsyncMock())
    ag = DiscoveryAgent(SimpleNamespace(page=SimpleNamespace()), client=SimpleNamespace(),
                        escalation_handler=handler, evidence_root=tmp_path)
    recorded, pending = [], []
    block = SimpleNamespace(name="request_human_input", id="t1",
                            input={"prompt": "Enter the 2FA code", "reason": "2fa"})
    r = await ag._handle_tool(block, "s3", 3, scope=None, consecutive={}, recorded=recorded,
                              pending_results=pending)
    assert r["status"] == "human_input"
    handler.escalate_planned.assert_awaited_once_with("Enter the 2FA code", "2fa")
    assert len(recorded) == 1
    step = recorded[0].action
    assert isinstance(step, HumanInputAction) and step.prompt == "Enter the 2FA code"
    assert pending and "Observe" in pending[0]["text"]


async def test_agent_request_human_input_timeout_is_soft(tmp_path):
    import asyncio
    handler = SimpleNamespace(escalate_planned=AsyncMock(side_effect=asyncio.TimeoutError()))
    ag = DiscoveryAgent(SimpleNamespace(page=SimpleNamespace()), client=SimpleNamespace(),
                        escalation_handler=handler, evidence_root=tmp_path)
    recorded, pending = [], []
    block = SimpleNamespace(name="request_human_input", id="t1", input={"prompt": "p", "reason": "r"})
    r = await ag._handle_tool(block, "s1", 1, None, {}, recorded, pending)
    assert r["status"] == "human_input" and len(recorded) == 1        # still records the step, doesn't crash
    assert "timed out" in pending[0]["text"]


# ---- contract 3: emission encodes HumanInputAction; prompt NOT reverse-parameterized ----------------
def test_human_input_prompt_not_reverse_parameterized():
    act = HumanInputAction(id="h", prompt="Enter code near account 12345", reason="2fa")
    new, used = reverse_parameterize_action(act, {"account_id": "12345"})
    assert new.prompt == "Enter code near account 12345"   # unchanged
    assert used == set()


def test_emit_artifact_with_human_input():
    steps = [RecordedStep("s1", HumanInputAction(id="h", prompt="Enter 2FA for 12345", reason="2fa"),
                          observation_after="ok")]
    art, dropped, warnings = emit_artifact("cap", steps, {}, [], model="m",
                                           capability_type=CapabilityType.read,
                                           caller_parameters={"account_id": "12345"})
    hstep = art.steps[0]
    assert hstep.action == "human_input" and hstep.prompt == "Enter 2FA for 12345"   # verbatim
    assert hstep.reason == "2fa" and hstep.timeout_ms == 60000


# ---- contract 4: capability_type = mutating when a human_input step is present ----------------------
def test_human_input_override_forces_mutating_over_declaration():
    steps = [NavigateAction(id="n", url="x"), HumanInputAction(id="h", prompt="p", reason="r")]
    # even declared read, a human_input step forces mutating (the override beats the declaration)
    assert apply_human_input_override(CapabilityType.read, steps) == CapabilityType.mutating
    from src.models import ReadTextAction, Locator
    steps2 = [ReadTextAction(id="r", locator=Locator(strategy="css_id", css="#b")),
              HumanInputAction(id="h", prompt="p", reason="r")]
    assert apply_human_input_override(CapabilityType.read, steps2) == CapabilityType.mutating


# ---- contract 5: validation gate SKIPS a human_input capability -------------------------------------
def _artifact_with_human_input() -> Artifact:
    steps = [RecordedStep("s1", HumanInputAction(id="h", prompt="Enter 2FA", reason="2fa"),
                          observation_after="ok")]
    art, _, _ = emit_artifact("needs_human", steps, {}, [], model="m", capability_type=CapabilityType.read)
    return art


def _dr(artifact) -> DiscoveryResult:
    return DiscoveryResult(status="success", artifact=artifact, capability_name="needs_human",
                           evidence_dir="ev", usage={"input": 1, "output": 1, "cache_read": 0, "cache_write": 0})


async def test_validation_gate_skipped_for_human_input(tmp_path, monkeypatch):
    ag = DiscoveryAgent(SimpleNamespace(page=SimpleNamespace()), client=SimpleNamespace(), evidence_root=tmp_path)
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_artifact_with_human_input())))
    monkeypatch.setattr(agent_mod, "run_validation_gate",
                        AsyncMock(side_effect=AssertionError("validation gate MUST be skipped")))
    res = await ag.discover_and_validate("g", "http://t/", "needs_human", capability_type=CapabilityType.mutating)
    assert res.validation_status == "skipped_requires_human"
    assert res.artifact.metadata.validation_skip_reason == "requires_human_input"
    assert res.artifact.metadata.validated is False


# ---- contract 6: replay executor dispatches human_input → escalate_planned --------------------------
async def test_executor_dispatch_human_input_calls_planned():
    handler = SimpleNamespace(escalate_planned=AsyncMock())
    executor = SimpleNamespace(escalation_handler=handler, page=SimpleNamespace(url="http://x"))
    action = HumanInputAction(id="h", prompt="Enter 2FA", reason="2fa", timeout_ms=5000)
    res = await action_dispatcher.dispatch(executor, action, scope=None)
    assert res.status == "success" and res.action == "human_input"
    # hint=None: this action carries no metadata.escalation_hint (ADR-007 revision passes it through).
    handler.escalate_planned.assert_awaited_once_with("Enter 2FA", "2fa", timeout_ms=5000, hint=None)


async def test_executor_dispatch_human_input_no_handler_raises():
    executor = SimpleNamespace(escalation_handler=None, page=SimpleNamespace(url="x"))
    with pytest.raises(ExecutorError):
        await action_dispatcher.dispatch(executor, HumanInputAction(id="h", prompt="p", reason="r"), scope=None)


# ---- contract 8: planned timeout → replay hard_failure; contract 9: engine wires the handler --------
async def test_executor_dispatch_timeout_status():
    import asyncio
    handler = SimpleNamespace(escalate_planned=AsyncMock(side_effect=asyncio.TimeoutError()))
    executor = SimpleNamespace(escalation_handler=handler, page=SimpleNamespace(url="http://x"))
    res = await action_dispatcher.dispatch(executor, HumanInputAction(id="h", prompt="p", reason="r"), scope=None)
    assert res.status == "human_input_timeout"


async def test_engine_wires_handler_and_timeout_is_hard_failure():
    art = Artifact(version="0.1.0",
                   metadata=ArtifactMetadata(capability_name="c", capability_type="mutating"),
                   steps=[HumanInputAction(id="h", prompt="p", reason="r")])
    executor = SimpleNamespace(
        escalation_handler=None,
        execute_action=AsyncMock(return_value=ActionResult(status="human_input_timeout", action="human_input")))
    handler = StubEscalationHandler()
    engine = ReplayEngine(executor, handler, lambda n: None)
    res = await engine.replay(art, {})
    assert executor.escalation_handler is handler                 # contract 9: engine wired it onto executor
    # Phase-3 D6: a planned-intervention timeout means no human acted -> technical_error subtype (not resolvable).
    assert res.status == "hard_failure" and res.reason == "technical_error:human_input_timeout"


async def test_engine_human_input_success_continues():
    art = Artifact(version="0.1.0",
                   metadata=ArtifactMetadata(capability_name="c", capability_type="mutating"),
                   steps=[HumanInputAction(id="h", prompt="p", reason="r")])
    executor = SimpleNamespace(
        escalation_handler=None,
        execute_action=AsyncMock(return_value=ActionResult(status="success", action="human_input")))
    engine = ReplayEngine(executor, StubEscalationHandler(), lambda n: None)
    res = await engine.replay(art, {})
    assert res.status == "success"                                # human_input step succeeded → replay done


# ---- contract 10: executor exposes get_current_page ------------------------------------------------
def test_executor_get_current_page():
    from src.executor import PlaywrightExecutor
    ex = PlaywrightExecutor(evidence_dir=".", headless=True)
    assert hasattr(ex, "get_current_page")
    assert ex.get_current_page() is None                          # None before start()
    ex.page = "PAGE"
    assert ex.get_current_page() == "PAGE"
