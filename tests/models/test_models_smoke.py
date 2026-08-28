"""Smoke tests for src/models (schema-draft.md authority).

Scope (deliberately shallow — deeper coverage is a later phase):
  1. Round-trip the two docs/schema-draft.md §10 worked examples: YAML -> models -> YAML -> models, and
     assert the models are structurally equal.
  2. One negative test per major validator, proving the validator actually fires.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.models import Artifact

# The two schema-draft §10 worked examples (10.1 fixture, 10.2 transfer) are committed here as fixtures,
# extracted verbatim, so the suite is self-contained — docs/schema-draft.md is an internal design draft and
# is not shipped in the repository.
_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _roundtrip(yaml_text: str) -> None:
    model1 = Artifact.model_validate(yaml.safe_load(yaml_text))
    dumped = model1.model_dump(mode="json", by_alias=True, exclude_none=True)
    model2 = Artifact.model_validate(yaml.safe_load(yaml.safe_dump(dumped)))
    assert model1 == model2


# ---------------- round-trip tests (the two §10 worked examples) ----------------

def test_roundtrip_fixture_example():
    _roundtrip(_fixture("register_user_and_open_savings.yaml"))


def test_roundtrip_transfer_example():
    _roundtrip(_fixture("transfer_between_accounts.yaml"))


def test_fixture_example_shape():
    """Sanity: the fixture parses to the expected high-level shape."""
    art = Artifact.model_validate(yaml.safe_load(_fixture("register_user_and_open_savings.yaml")))
    assert art.metadata.capability_name == "register_user_and_open_savings"
    assert art.metadata.capability_type.value == "mutating"
    assert {c.name for c in art.captures} == {"checking_account_id", "account_ids", "savings_account_id"}
    # find_matching step present with candidate binding
    fm = [s for s in art.steps if getattr(s, "action", None) == "find_matching"]
    assert fm and fm[0].capture.value_from == "candidate"


def test_transfer_sample_invocation_targets_exist():
    """Cross-field sanity (ADR-9): every sample_invocation key is a declared parameter of the capability."""
    art = Artifact.model_validate(yaml.safe_load(_fixture("transfer_between_accounts.yaml")))
    params = set(art.parameters.properties.keys())
    assert art.metadata.sample_invocation is not None
    assert set(art.metadata.sample_invocation.keys()) <= params


# ---------------- negative tests (one per major validator) ----------------

def _valid_min() -> dict:
    return {
        "version": "0.1.0",
        "metadata": {"capability_name": "cap", "capability_type": "read"},
        "parameters": {"type": "object", "properties": {"p": {"type": "string"}}, "required": []},
        "captures": [{"name": "c", "type": "string", "source_step": "s1"}],
        "steps": [{"id": "s1", "action": "navigate", "url": "overview.htm"}],
    }


def _valid_find_matching() -> dict:
    return {
        "version": "0.1.0",
        "metadata": {"capability_name": "cap", "capability_type": "read"},
        "captures": [
            {"name": "ids", "type": "string[]", "source_step": "s1"},
            {"name": "v", "type": "string", "source_step": "s2"},
        ],
        "steps": [
            {"id": "s1", "action": "navigate", "url": "overview.htm"},
            {
                "id": "s2",
                "action": "find_matching",
                "candidates": "ids",
                "probe": {
                    "action": "navigate",
                    "locator": {"strategy": "url_template", "url_template": "x?id={{candidate}}"},
                    "checkpoint": {"success": {"required_phrases": ["SAVINGS"]}},
                },
                "capture": {"variable": "v", "value_from": "candidate"},
            },
        ],
    }


def test_baseline_valid():
    Artifact.model_validate(_valid_min())
    Artifact.model_validate(_valid_find_matching())


def test_invalid_capability_type():
    d = _valid_min()
    d["metadata"]["capability_type"] = "readonly"  # not in enum
    with pytest.raises(ValidationError):
        Artifact.model_validate(d)


def test_capture_shadows_parameter():
    d = _valid_min()
    d["captures"][0]["name"] = "p"  # shadows parameter 'p'
    with pytest.raises(ValidationError, match="shadow"):
        Artifact.model_validate(d)


def test_duplicate_capture_names():
    d = _valid_min()
    d["captures"] = [
        {"name": "c", "type": "string", "source_step": "s1"},
        {"name": "c", "type": "string", "source_step": "s1"},
    ]
    with pytest.raises(ValidationError, match="duplicate capture"):
        Artifact.model_validate(d)


def test_unresolved_interpolation():
    d = _valid_min()
    d["steps"] = [{"id": "s1", "action": "type_text", "locator": {"css": "#x"}, "value": "{{missing}}"}]
    with pytest.raises(ValidationError, match="does not resolve"):
        Artifact.model_validate(d)


def test_capture_source_step_missing():
    d = _valid_min()
    d["captures"][0]["source_step"] = "nope"
    with pytest.raises(ValidationError, match="not a declared step id"):
        Artifact.model_validate(d)


def test_sample_invocation_roundtrips():
    """ADR-9: metadata.sample_invocation is an optional dict[str,str] that survives a Pydantic round-trip."""
    d = _valid_min()
    d["metadata"]["sample_invocation"] = {"account_id": "12345"}
    art = Artifact.model_validate(d)
    assert art.metadata.sample_invocation == {"account_id": "12345"}
    # round-trip through dump -> validate
    art2 = Artifact.model_validate(art.model_dump())
    assert art2.metadata.sample_invocation == {"account_id": "12345"}
    # absent -> None
    d.pop("sample_invocation", None)
    d["metadata"].pop("sample_invocation")
    assert Artifact.model_validate(d).metadata.sample_invocation is None


def test_find_matching_value_from_must_be_candidate():
    d = _valid_find_matching()
    d["steps"][1]["capture"]["value_from"] = "other"
    with pytest.raises(ValidationError):
        Artifact.model_validate(d)


def test_find_matching_probe_requires_checkpoint():
    d = _valid_find_matching()
    del d["steps"][1]["probe"]["checkpoint"]
    with pytest.raises(ValidationError):
        Artifact.model_validate(d)


def test_find_matching_candidates_must_be_capture():
    d = _valid_find_matching()
    d["steps"][1]["candidates"] = "not_a_capture"
    with pytest.raises(ValidationError, match="not a declared capture"):
        Artifact.model_validate(d)


def test_checkpoint_wait_ms_ge_poll_interval():
    d = _valid_min()
    d["steps"][0]["checkpoint"] = {
        "success": {"required_phrases": ["ok"]},
        "wait_ms": 100,
        "poll_interval_ms": 500,
    }
    with pytest.raises(ValidationError, match="poll_interval_ms"):
        Artifact.model_validate(d)


def test_invalid_generate_marker():
    d = _valid_min()
    d["parameters"]["properties"]["p"] = {"type": "string", "generate": "unique_foo"}
    with pytest.raises(ValidationError):
        Artifact.model_validate(d)


def test_generate_and_default_mutually_exclusive():
    d = _valid_min()
    d["parameters"]["properties"]["p"] = {"type": "string", "generate": "unique_string", "default": "x"}
    with pytest.raises(ValidationError, match="generate"):
        Artifact.model_validate(d)


def test_extra_field_forbidden():
    d = _valid_min()
    d["bogus_field"] = 1
    with pytest.raises(ValidationError):
        Artifact.model_validate(d)


def test_human_input_step_roundtrips():
    """ADR-007 planned mode: a human_input step validates + round-trips through Pydantic (prompt verbatim)."""
    d = {
        "version": "0.1.0",
        "metadata": {"capability_name": "needs_human", "capability_type": "mutating"},
        "steps": [{"id": "h", "action": "human_input", "prompt": "Enter the 2FA code", "reason": "2fa"}],
    }
    art = Artifact.model_validate(d)
    assert art.steps[0].action == "human_input"
    assert art.steps[0].prompt == "Enter the 2FA code" and art.steps[0].timeout_ms == 60000
    art2 = Artifact.model_validate(art.model_dump())
    assert art2.steps[0].prompt == "Enter the 2FA code"


def test_validation_skip_reason_roundtrips():
    d = _valid_min()
    d["metadata"]["validation_skip_reason"] = "requires_human_input"
    art = Artifact.model_validate(d)
    assert art.metadata.validation_skip_reason == "requires_human_input"
    assert Artifact.model_validate(art.model_dump()).metadata.validation_skip_reason == "requires_human_input"


def test_sample_invocation_ref_roundtrips():
    """sample_invocation values may be '$json:dot.path' references (still plain dict[str,str])."""
    d = {
        "version": "0.1.0",
        "metadata": {"capability_name": "c", "capability_type": "read",
                     "sample_invocation": {"account_id": "$json:primary.checking_id"}},
        "steps": [{"id": "s1", "action": "navigate", "url": "x"}],
    }
    art = Artifact.model_validate(d)
    assert art.metadata.sample_invocation == {"account_id": "$json:primary.checking_id"}
    assert Artifact.model_validate(art.model_dump()).metadata.sample_invocation["account_id"].startswith("$json:")
