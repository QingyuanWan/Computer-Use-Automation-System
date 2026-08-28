"""Credential parameterization tests (ADR-010): B2 auto-parameterization, B1 refuse-to-emit, the
no-false-positive guarantees (Q1), and the discovery system-prompt guidance (Q2). Pure functions — no LLM."""
from __future__ import annotations

import pytest

from src.agent.credentials import (
    CredentialsHardcodedError,
    detect_credential,
    looks_like_credential_value,
)
from src.agent.emission import emit_artifact
from src.agent.results import RecordedStep
from src.agent.tools import build_system_blocks
from src.models import CapabilityType, Locator, NavigateAction, TypeTextAction


def _tt(value, **loc):
    loc.setdefault("strategy", "css")
    if loc["strategy"] == "css":
        loc.setdefault("css", "#f")
    return TypeTextAction(id="t", locator=Locator(**loc), value=value)


def _rs(action):
    return RecordedStep("s1", action, observation_after="ok")


# ---------------- detection unit (field-name α only) ----------------

def test_detect_credential_names():
    assert detect_credential("username") == "username"
    assert detect_credential("user_name") == "username"
    assert detect_credential("Password") == "password"
    assert detect_credential(None, None, "Login") == "username"
    assert detect_credential("ssn") == "ssn"
    # no false positives on non-credential ids (Q1: 'account' deliberately excluded)
    assert detect_credential("member_id") is None
    assert detect_credential("account_id") is None
    assert detect_credential("amount") is None


def test_looks_like_credential_value():
    assert looks_like_credential_value("Reg1stry#Pw2026") is True     # letters+digits, len>=6
    assert looks_like_credential_value("itfai_3824e30a") is True
    assert looks_like_credential_value("12345") is False              # too short / no letters
    assert looks_like_credential_value("John") is False               # no digit
    assert looks_like_credential_value("100.00") is False             # no letter
    assert looks_like_credential_value("{{username}}") is False       # a placeholder is never a credential


# ---------------- B2: auto-parameterize named credential fields ----------------

def test_b2_autoparam_username_by_name():
    art, _, _ = emit_artifact("cap", [_rs(_tt("alice_2024", name="username"))], {}, [], model="m", capability_type=CapabilityType.read)
    assert art.steps[0].value == "{{username}}"
    assert art.parameters.properties["username"].sensitive is True
    assert "username" in art.parameters.required


def test_b2_autoparam_password_by_label():
    step = _rs(_tt("hunter2X", strategy="role_nth", role="textbox", index=1, label="Password"))
    art, _, _ = emit_artifact("cap", [step], {}, [], model="m", capability_type=CapabilityType.read)
    assert art.steps[0].value == "{{password}}"
    assert art.parameters.properties["password"].sensitive is True


# ---------------- no false positives (Q1) ----------------

def test_no_falsepos_member_id_numeric():
    art, _, _ = emit_artifact("cap", [_rs(_tt("12345", name="member_id"))], {}, [], model="m", capability_type=CapabilityType.read)
    assert art.steps[0].value == "12345"          # NOT parameterized (not a credential name; value not shaped)
    assert art.parameters is None


def test_b2_noop_when_value_is_caller_param():
    # value equals a caller parameter -> Q2 reverse-param templates it; B2 sees {{ and does nothing
    art, _, _ = emit_artifact("cap", [_rs(_tt("12345"))], {}, [], model="m", capability_type=CapabilityType.read,
                              caller_parameters={"account_id": "12345"})
    assert art.steps[0].value == "{{account_id}}"
    assert "username" not in art.parameters.properties and "password" not in art.parameters.properties


# ---------------- B1: refuse-to-emit safety net ----------------

def test_b1_refuses_hardcoded_credential_and_redacts_value():
    # nameless field (like ParaBank's role_nth login inputs) + credential-shaped literal -> B1 fires
    step = _rs(_tt("Reg1stry#Pw2026", strategy="role_nth", role="textbox", index=0))
    with pytest.raises(CredentialsHardcodedError) as ei:
        emit_artifact("cap", [step], {}, [], model="m", capability_type=CapabilityType.read)
    assert "Reg1stry#Pw2026" not in str(ei.value)     # the secret is never echoed in the error
    assert "s1" in str(ei.value)                       # but the suspect step is named


def test_b1_silent_when_b2_parameterized():
    # same shaped value, but the field IS credential-named -> B2 handles it -> B1 does not fire
    art, _, _ = emit_artifact("cap", [_rs(_tt("Reg1stry#Pw2026", name="password"))], {}, [], model="m", capability_type=CapabilityType.read)
    assert art.steps[0].value == "{{password}}"        # no raise


# ---------------- caller-param credentials marked sensitive (Task-3 path) ----------------

def test_caller_param_credentials_marked_sensitive():
    # username/password supplied as caller params -> reverse-parameterized + sensitive; account_id is not
    step = _rs(_tt("alice", strategy="role_nth", role="textbox", index=0))
    art, _, _ = emit_artifact(
        "cap", [step, RecordedStep("s2", NavigateAction(id="n", url="activity.htm?id=12345"),
                                   observation_after="ok")],
        {}, [], model="m", capability_type=CapabilityType.read,
        caller_parameters={"username": "alice", "password": "s3cretPW", "account_id": "12345"},
        caller_parameter_sources={"username": "$json:primary.username", "password": "$json:primary.password",
                                  "account_id": "$json:primary.checking_id"})
    props = art.parameters.properties
    assert props["username"].sensitive is True and props["password"].sensitive is True
    assert props["account_id"].sensitive is None
    assert art.steps[0].value == "{{username}}"
    assert art.metadata.sample_invocation["password"] == "$json:primary.password"


# ---------------- Q2: system prompt guidance ----------------

def test_system_prompt_requires_human_input_for_missing_credentials():
    text = build_system_blocks("some goal", "http://t/")[0]["text"]
    assert "request_human_input" in text
    assert "credential" in text.lower()
    assert "not invent" in text.lower()
