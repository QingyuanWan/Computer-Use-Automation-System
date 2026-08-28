"""Registry-referenced sample_invocation ($json:dot.path resolved against the JSON credential store at
validation time). All mocked — no browser/LLM/network."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.agent.validation_gate as vg
import src.registry as registry
from src.agent.emission import emit_artifact
from src.agent.results import RecordedStep
from src.agent.validation_gate import resolve_sample_invocation, run_validation_gate
from src.models import CapabilityType, NavigateAction
from src.replay import ReplayResult

_CREDS = {
    "url": "http://t/",
    "primary": {"username": "u", "password": "p", "checking_id": "15564", "savings_id": "15675"},
    "invalid_account_id": "999999999",
}


def _read_artifact(literal="13899", sources=None):
    steps = [RecordedStep("s1", NavigateAction(id="n", url=f"activity.htm?id={literal}"), observation_after="ok")]
    art, _, _ = emit_artifact("cap", steps, {}, [], model="m", capability_type=CapabilityType.read,
                              caller_parameters={"account_id": literal}, caller_parameter_sources=sources)
    return art


# ---- emission records the $json ref (not a literal) when a source is provided -----------------------
def test_emission_records_json_ref():
    art = _read_artifact(sources={"account_id": "$json:primary.checking_id"})
    assert art.metadata.sample_invocation == {"account_id": "$json:primary.checking_id"}
    assert art.steps[0].url == "activity.htm?id={{account_id}}"   # step still parameterized against literal


# ---- edge case + backward compat: literal caller_parameters (no sources) → literal sample_invocation --
def test_emission_literal_fallback_when_no_source():
    art = _read_artifact(sources=None)
    assert art.metadata.sample_invocation == {"account_id": "13899"}   # literal preserved (ADR-9 behavior)


# ---- resolver resolves $json refs against the store; passes literals through ------------------------
def test_resolve_json_ref():
    resolved, missing = resolve_sample_invocation(
        {"account_id": "$json:primary.checking_id", "amount": "25"}, creds=_CREDS)
    assert resolved == {"account_id": "15564", "amount": "25"} and missing == []


def test_resolve_json_ref_missing_path():
    resolved, missing = resolve_sample_invocation({"a": "$json:primary.nope"}, creds=_CREDS)
    assert missing == ["primary.nope"]


def test_resolve_literal_lookalike_stays_literal():
    # a value that is NOT the strict $json:dotpath form (has a space) is treated as a literal
    resolved, missing = resolve_sample_invocation({"a": "$json:not valid", "b": "plain"}, creds=_CREDS)
    assert resolved == {"a": "$json:not valid", "b": "plain"} and missing == []


# ---- validation gate resolves against the CURRENT registry (picks up a changed value) ---------------
def _fake_gate(monkeypatch, captured):
    class FakeExec:
        def __init__(self, **kw):
            self.page = SimpleNamespace(goto=AsyncMock())

        async def start(self):
            ...

        async def stop(self):
            ...

    class FakeEngine:
        def __init__(self, ex, h, loader):
            ...

        async def replay(self, artifact, caller_parameters):
            captured["cp"] = caller_parameters
            return ReplayResult.success({})

    monkeypatch.setattr(vg, "PlaywrightExecutor", FakeExec)
    monkeypatch.setattr(vg, "ReplayEngine", FakeEngine)


async def test_validation_gate_resolves_current_registry(monkeypatch, tmp_path):
    captured = {}
    _fake_gate(monkeypatch, captured)
    store = {"primary": {"checking_id": "77777"}}                  # "current" registry value
    monkeypatch.setattr(registry, "load_credentials", lambda *a, **k: store)
    art = _read_artifact(sources={"account_id": "$json:primary.checking_id"})  # stores the reference
    res = await run_validation_gate(art, target_url="http://t/", evidence_dir=tmp_path)
    assert res.status == "success"
    assert captured["cp"] == {"account_id": "77777"}              # resolved at replay, NOT a stale literal
    # change the registry value → the gate uses the new one (proves re-resolution)
    monkeypatch.setattr(registry, "load_credentials", lambda *a, **k: {"primary": {"checking_id": "88888"}})
    await run_validation_gate(art, target_url="http://t/", evidence_dir=tmp_path)
    assert captured["cp"] == {"account_id": "88888"}


async def test_validation_gate_missing_path_hard_failure(monkeypatch, tmp_path):
    captured = {}
    _fake_gate(monkeypatch, captured)
    monkeypatch.setattr(registry, "load_credentials", lambda *a, **k: {"primary": {}})
    art = _read_artifact(sources={"account_id": "$json:primary.checking_id"})
    res = await run_validation_gate(art, target_url="http://t/", evidence_dir=tmp_path)
    assert res.status == "hard_failure" and res.reason == "credential_path_missing:primary.checking_id"
    assert "cp" not in captured                                   # replay never invoked


async def test_validation_gate_literal_sample_invocation_still_works(monkeypatch, tmp_path):
    captured = {}
    _fake_gate(monkeypatch, captured)
    art = _read_artifact(sources=None)                           # literal sample_invocation {account_id: 13899}
    res = await run_validation_gate(art, target_url="http://t/", evidence_dir=tmp_path)
    assert res.status == "success" and captured["cp"] == {"account_id": "13899"}
