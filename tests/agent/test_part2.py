"""Unit tests for Part 2: failure-injection sub-run + auto-replay validation gate.

No LLM, no Playwright — self.discover, run_failure_injection, run_validation_gate, PlaywrightExecutor and
ReplayEngine are all mocked. Covers the Part 2 8-item behavior contract.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.agent.agent as agent_mod
import src.agent.validation_gate as vg_mod
from src.agent import DiscoveryAgent
from src.agent.agent import _billed
from src.models import CapabilityType
from src.agent.emission import emit_artifact
from src.agent.failure_injection import classify_injection, run_failure_injection
from src.agent.results import DiscoveryResult, RecordedStep
from src.models import (
    ClickAction,
    ExpectedOutcome,
    Locator,
    NavigateAction,
    ReadTextAction,
    TypeTextAction,
)
from src.replay import ReplayResult


# ---------------- fixtures / helpers ----------------

def _nav():
    return NavigateAction(id="n", url="overview.htm")


def _read():
    return ReadTextAction(id="r", locator=Locator(strategy="css_id", css="#b"))


def _tt(v="Ada"):
    return TypeTextAction(id="t", locator=Locator(strategy="css_id", css="#f"), value=v)


def _click(name):
    return ClickAction(id="c", locator=Locator(strategy="role_name", role="button", name=name))


def _read_artifact():
    steps = [RecordedStep("s1", _nav(), observation_after="Accounts Overview 12345"),
             RecordedStep("s2", _read(), observation_after="Account Details — Balance: $415.50",
                          read_text_value="$415.50")]
    art, _, _ = emit_artifact("lookup_balance", steps, {"balance": "$415.50"},
                              ["Account Details", "Balance: $415.50"], model="m",
                              capability_type=CapabilityType.read)
    return art


def _mutating_artifact():
    steps = [RecordedStep("s1", _tt("Ada"), observation_after="Welcome Ada — created successfully"),
             RecordedStep("s2", _click("Register"), observation_after="Welcome Ada — created successfully")]
    art, _, _ = emit_artifact("register_user", steps, {"user": "Ada"}, ["created successfully"], model="m",
                              capability_type=CapabilityType.mutating)
    return art


def _param_artifact(caller_parameters):
    """A read capability whose account_id is caller-supplied (ADR-9): declares parameters + sample_invocation."""
    acct = caller_parameters["account_id"]
    steps = [RecordedStep("s1", NavigateAction(id="n", url=f"activity.htm?id={acct}"),
                          observation_after="Account Details — Balance: $415.50"),
             RecordedStep("s2", _read(), observation_after="Account Details — Balance: $415.50",
                          read_text_value="$415.50")]
    art, _, _ = emit_artifact("lookup_balance", steps, {"balance": "$415.50"}, ["Account Details"],
                              model="m", capability_type=CapabilityType.read,
                              caller_parameters=caller_parameters)
    return art


def _dr(artifact, status="success", usage=None, evidence_dir="ev"):
    return DiscoveryResult(status=status, artifact=artifact, capability_name="cap", evidence_dir=evidence_dir,
                           usage=usage or {"input": 100, "output": 40, "cache_read": 5000, "cache_write": 200})


def _make_agent(tmp_path):
    ag = DiscoveryAgent(SimpleNamespace(page=SimpleNamespace()), client=SimpleNamespace(),
                        evidence_root=tmp_path)
    return ag


class _FakePage:
    def __init__(self, status, aria, url):
        self._status, self._aria, self.url = status, aria, url

    async def evaluate(self, _script):
        return self._status

    def locator(self, _sel):
        loc = MagicMock()
        loc.aria_snapshot = AsyncMock(return_value=self._aria)
        return loc


# ================= Contract 1: injection runs ONLY for read (write/mutating skips + logs reason) =================

async def test_contract1_mutating_skips_injection(tmp_path, monkeypatch):
    ag = _make_agent(tmp_path)
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_mutating_artifact())))
    inj_spy = AsyncMock(side_effect=AssertionError("injection must NOT run for mutating"))
    monkeypatch.setattr(agent_mod, "run_failure_injection", inj_spy)
    gate_spy = AsyncMock(side_effect=AssertionError("validation gate must NOT run for mutating (replay IS the mutation)"))
    monkeypatch.setattr(agent_mod, "run_validation_gate", gate_spy)

    res = await ag.discover_and_validate("g", "http://t/", "cap", capability_type=CapabilityType.read)

    assert res.injection_ran is False
    assert res.injection_skip_reason is not None and "mutating" in res.injection_skip_reason
    inj_spy.assert_not_called()                 # guard fires BEFORE any injection action
    # (a) fix: a mutating capability is NOT auto-validated — the validation replay IS the mutation.
    gate_spy.assert_not_called()
    assert res.validation_status == "skipped_mutating"
    assert res.artifact.metadata.validation_skip_reason == "mutating_not_auto_validatable"
    assert res.artifact.metadata.validated is False


async def test_contract1_read_invokes_injection(tmp_path, monkeypatch):
    ag = _make_agent(tmp_path)
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_read_artifact())))
    outcome = ExpectedOutcome(name="account_not_found",
                              required_phrases=["Error! Could not find account # 999999999"])
    monkeypatch.setattr(agent_mod, "run_failure_injection",
                        AsyncMock(return_value=([outcome], [{"input": 10, "output": 5, "cache_write": 20}], [])))
    monkeypatch.setattr(agent_mod, "run_validation_gate", AsyncMock(return_value=ReplayResult.success({})))

    res = await ag.discover_and_validate("g", "http://t/", "cap", capability_type=CapabilityType.read)

    assert res.injection_ran is True
    assert res.expected_outcomes_added == 1
    # Contract 3: the entry is NAMED and carries failure phrases, attached to the terminal checkpoint step.
    cps = [s.checkpoint for s in res.artifact.steps if getattr(s, "checkpoint", None) is not None]
    eos = cps[-1].expected_outcomes
    assert [e.name for e in eos] == ["account_not_found"]
    assert eos[0].required_phrases == ["Error! Could not find account # 999999999"]


# ===== (a) fix: the validation gate is a real replay, so it must not run for a mutating capability =====

async def test_mutating_capability_skips_validation_gate(tmp_path, monkeypatch):
    """A mutating capability cannot be auto-validated by replaying it — the validation replay IS the mutation.
    So the gate is skipped (validated=false + principled skip reason), and it is NOT invoked."""
    ag = _make_agent(tmp_path)
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_mutating_artifact())))
    monkeypatch.setattr(agent_mod, "run_failure_injection", AsyncMock(return_value=([], [], [])))
    gate = AsyncMock(return_value=ReplayResult.success({}))
    monkeypatch.setattr(agent_mod, "run_validation_gate", gate)

    res = await ag.discover_and_validate("g", "http://t/", "cap", capability_type=CapabilityType.mutating)

    gate.assert_not_called()
    assert res.validation_status == "skipped_mutating"
    assert res.artifact.metadata.validation_skip_reason == "mutating_not_auto_validatable"
    assert res.artifact.metadata.validated is False


async def test_read_capability_still_runs_validation_gate(tmp_path, monkeypatch):
    """The other direction: a read capability still runs the gate (a read replay has no side effect), and a
    passing gate flips validated=true."""
    ag = _make_agent(tmp_path)
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_read_artifact())))
    monkeypatch.setattr(agent_mod, "run_failure_injection", AsyncMock(return_value=([], [], [])))
    gate = AsyncMock(return_value=ReplayResult.success({}))
    monkeypatch.setattr(agent_mod, "run_validation_gate", gate)

    res = await ag.discover_and_validate("g", "http://t/", "cap", capability_type=CapabilityType.read)

    gate.assert_called_once()
    assert res.validation_status == "success"
    assert res.artifact.metadata.validated is True


# ================= Contract 2: 3-condition classifier discards silently on ANY failed condition =================

async def test_contract2_classifier_admits_valid():
    page = _FakePage(200, '- text "Error! Could not find account # 999999999"',
                     "https://bank/parabank/activity.htm?id=999999999")
    ok, phrases = await classify_injection(page, "999999999", {})
    assert ok is True
    assert phrases == ["Error! Could not find account # 999999999"]


@pytest.mark.parametrize("status,aria,url,why", [
    (500, '- text "Could not find account # 999999999"', "https://bank/parabank/activity.htm?id=999999999",
     "http 500 fails condition 1"),
    (200, '- text "Welcome, everything is fine"', "https://bank/parabank/activity.htm?id=999999999",
     "injected value absent fails condition 2"),
    (200, '- text "Error! Could not find account # 999999999"', "https://bank/parabank/error.htm",
     "generic error route fails condition 3"),
])
async def test_contract2_classifier_discards_on_each_failed_condition(status, aria, url, why):
    page = _FakePage(status, aria, url)
    ok, phrases = await classify_injection(page, "999999999", {})
    assert ok is False, why
    assert phrases == []


async def test_contract2_run_injection_discards_bad_subrun(tmp_path, monkeypatch):
    # a valid strategy admits; the other's final page fails condition 2 -> only one expected_outcome.
    ag = _make_agent(tmp_path)
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_read_artifact())))
    pages = iter([
        _FakePage(200, '- text "Error! Could not find account # 999999999"',
                  "https://bank/parabank/activity.htm?id=999999999"),      # strat 1 admits
        _FakePage(200, '- text "all good, nothing wrong here"',
                  "https://bank/parabank/activity.htm?id=notanumber"),      # strat 2 discarded (cond 2)
    ])
    ag.executor = SimpleNamespace(page=None)

    async def fake_discover(goal, url, name, caller_parameters=None, *, capability_type=None, generate_hints=True):
        # generate_hints kwarg mirrors the real discover(): failure_injection passes generate_hints=False.
        ag.executor.page = next(pages)
        return _dr(_read_artifact())

    monkeypatch.setattr(ag, "discover", AsyncMock(side_effect=fake_discover))
    outcomes, usages, warnings = await run_failure_injection(ag, _read_artifact(), "g", "http://t/", "cap")
    assert [o.name for o in outcomes] == ["account_not_found"]
    assert any("discarded" in w for w in warnings)
    assert len(usages) == 2


# ================= Contract 4: injection phrases are reverse-parameterized (ADR-5 Gap #2) =================

async def test_contract4_injection_phrase_reverse_parameterized():
    page = _FakePage(200, '- text "Error! Could not find account # 999999999"',
                     "https://bank/parabank/activity.htm?id=999999999")
    ok, phrases = await classify_injection(page, "999999999", {"account_id": "999999999"})
    assert ok is True
    assert phrases == ["Error! Could not find account # {{account_id}}"]   # value -> {{name}}


# ================= Contract 5: validation gate uses a FRESH executor (not the discovery session) =================

async def test_contract5_validation_uses_fresh_executor(tmp_path, monkeypatch):
    started = {"start": 0, "stop": 0, "goto": None, "instances": 0}

    class FakeExecutor:
        def __init__(self, *, evidence_dir, base_url, headless):
            started["instances"] += 1
            self.page = SimpleNamespace(goto=AsyncMock(side_effect=lambda u: started.__setitem__("goto", u)))

        async def start(self):
            started["start"] += 1

        async def stop(self):
            started["stop"] += 1

    captured = {}

    class FakeEngine:
        def __init__(self, executor, handler, loader):
            captured["executor"] = executor
            captured["loader"] = loader

        async def replay(self, artifact, caller_parameters):
            captured["artifact"] = artifact
            captured["caller_parameters"] = caller_parameters
            return ReplayResult.success({})

    monkeypatch.setattr(vg_mod, "PlaywrightExecutor", FakeExecutor)
    monkeypatch.setattr(vg_mod, "ReplayEngine", FakeEngine)

    art = _read_artifact()
    res = await vg_mod.run_validation_gate(art, target_url="https://bank/parabank/index.htm",
                                           evidence_dir=tmp_path)
    assert res.status == "success"
    assert started["instances"] == 1 and started["start"] == 1 and started["stop"] == 1   # fresh + lifecycle
    assert started["goto"] == "https://bank/parabank/index.htm"                            # navigated first
    assert isinstance(captured["executor"], FakeExecutor)                                   # engine got the fresh one


# ================= Contract 6 (ADR-9): validation gate uses sample_invocation as caller_parameters =========

def _fake_vg(monkeypatch, captured):
    class FakeExecutor:
        def __init__(self, **kw):
            self.page = SimpleNamespace(goto=AsyncMock())

        async def start(self):
            ...

        async def stop(self):
            ...

    class FakeEngine:
        def __init__(self, executor, handler, loader):
            ...

        async def replay(self, artifact, caller_parameters):
            captured["artifact"] = artifact
            captured["caller_parameters"] = caller_parameters
            return ReplayResult.success({})

    monkeypatch.setattr(vg_mod, "PlaywrightExecutor", FakeExecutor)
    monkeypatch.setattr(vg_mod, "ReplayEngine", FakeEngine)


async def test_contract6_gate_passes_sample_invocation_to_replay(tmp_path, monkeypatch):
    captured = {}
    _fake_vg(monkeypatch, captured)
    art = _param_artifact({"account_id": "12345"})    # declares a param + sample_invocation
    res = await vg_mod.run_validation_gate(art, target_url="http://t/", evidence_dir=tmp_path)
    assert res.status == "success"
    assert captured["caller_parameters"] == {"account_id": "12345"}   # sample_invocation -> replay params
    assert captured["artifact"] is art                                # artifact passed through unchanged


async def test_contract6_gate_fails_when_params_but_no_sample_invocation(tmp_path, monkeypatch):
    captured = {}
    _fake_vg(monkeypatch, captured)
    art = _param_artifact({"account_id": "12345"}).model_copy(
        update={"metadata": _param_artifact({"account_id": "12345"}).metadata.model_copy(
            update={"sample_invocation": None})})
    res = await vg_mod.run_validation_gate(art, target_url="http://t/", evidence_dir=tmp_path)
    assert res.status == "hard_failure"
    assert "sample_invocation" in res.reason
    assert "artifact" not in captured           # replay never invoked


async def test_contract6_fixture_case_no_params_validates_via_generator(tmp_path, monkeypatch):
    # a self-contained capability (no parameters, e.g. a generate-only fixture) has sample_invocation=None
    # and still validates: the gate replays with {} and the replay engine's generator handles synthesis.
    captured = {}
    _fake_vg(monkeypatch, captured)
    art = _read_artifact()                        # no parameters block, sample_invocation None
    assert art.parameters is None and art.metadata.sample_invocation is None
    res = await vg_mod.run_validation_gate(art, target_url="http://t/", evidence_dir=tmp_path)
    assert res.status == "success"
    assert captured["caller_parameters"] == {}    # replayed with empty params, generator does the rest


# ================= Contract 7: validated flips true on success only =================

@pytest.mark.parametrize("replay_result,expected", [
    (ReplayResult.success({}), True),
    (ReplayResult.business_outcome("account_not_found"), False),
    (ReplayResult.hard_failure(reason="locator vanished"), False),
])
async def test_contract7_validated_flip(tmp_path, monkeypatch, replay_result, expected):
    ag = _make_agent(tmp_path)
    # Use a READ artifact so the validation gate actually runs (a mutating one now SKIPS the gate — the (a)
    # fix); mock injection so it is a no-op. validated must flip true iff the gate result is success.
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_read_artifact())))
    monkeypatch.setattr(agent_mod, "run_failure_injection", AsyncMock(return_value=([], [], [])))
    monkeypatch.setattr(agent_mod, "run_validation_gate", AsyncMock(return_value=replay_result))

    res = await ag.discover_and_validate("g", "http://t/", "cap", capability_type=CapabilityType.read)
    assert res.artifact.metadata.validated is expected
    assert res.validation_status == replay_result.status


# ================= Contract 8: cumulative billed tokens across all sub-runs =================

async def test_contract8_cumulative_token_cost(tmp_path, monkeypatch):
    ag = _make_agent(tmp_path)
    happy = {"input": 100, "output": 40, "cache_read": 9000, "cache_write": 200}
    inj1 = {"input": 20, "output": 10, "cache_read": 3000, "cache_write": 50}
    inj2 = {"input": 30, "output": 15, "cache_read": 4000, "cache_write": 60}
    monkeypatch.setattr(ag, "discover", AsyncMock(return_value=_dr(_read_artifact(), usage=happy)))
    monkeypatch.setattr(agent_mod, "run_failure_injection", AsyncMock(return_value=([], [inj1, inj2], [])))
    monkeypatch.setattr(agent_mod, "run_validation_gate", AsyncMock(return_value=ReplayResult.success({})))

    res = await ag.discover_and_validate("g", "http://t/", "cap", capability_type=CapabilityType.read)

    assert res.total_billed_tokens == _billed(happy) + _billed(inj1) + _billed(inj2)
    assert res.usage == {"input": 150, "output": 65, "cache_read": 16000, "cache_write": 310}  # summed, once each
