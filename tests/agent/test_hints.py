"""Unit tests for escalation-hint generation + attachment (ADR-007 revision; D1-α / D2-β).

Pure helpers (is_hint_worthy, attach_hints) are tested without the LLM; generate_step_hints is tested with a
mock Anthropic client. Covers behavior-contract items 1 (hint-worthy filter), 2 (bounded prompt),
3 (reverse-parameterization), 4 (model field), backward compat.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent import hints
from src.models import (
    Artifact,
    ArtifactMetadata,
    CapabilityType,
    Checkpoint,
    ClickAction,
    HumanInputAction,
    Locator,
    NavigateAction,
    ReadTextAction,
    StepMetadata,
    SuccessCriteria,
    TypeTextAction,
)


def _artifact(steps):
    return Artifact(
        version="0.1.0",
        metadata=ArtifactMetadata(capability_name="cap", capability_type=CapabilityType.read,
                                  validated=False, discovered_by_model="m"),
        steps=steps)


def _login_and_lookup_steps():
    # literal fields only (no undeclared {{...}}) — hint-worthiness depends on action type / checkpoint, not on
    # the field values. The reverse-parameterization test operates on the HINT text, not on these steps.
    cp = Checkpoint(success=SuccessCriteria(required_phrases=["Account Details", "CHECKING"]))
    return [
        TypeTextAction(id="step_01_0", locator=Locator(strategy="role_nth", role="textbox", index=0),
                       value="user"),                                          # NOT hint-worthy
        TypeTextAction(id="step_01_1", locator=Locator(strategy="role_nth", role="textbox", index=1),
                       value="pass"),                                          # NOT hint-worthy
        ClickAction(id="step_02", locator=Locator(strategy="role_name", role="button", name="Log In")),  # NOT
        NavigateAction(id="step_03", url="activity.htm?id=123", checkpoint=cp),  # hint-worthy (navigate + cp)
        ReadTextAction(id="step_04", locator=Locator(strategy="css_id", css="#balance")),   # NOT hint-worthy
    ]


# ---------------- item 1 (D2-β): hint-worthy filter ----------------

def test_is_hint_worthy_by_action_type():
    nav = NavigateAction(id="n", url="x")
    hi = HumanInputAction(id="h", prompt="p", reason="r")
    assert hints.is_hint_worthy(nav) is True
    assert hints.is_hint_worthy(hi) is True


def test_is_hint_worthy_by_checkpoint():
    cp = Checkpoint(success=SuccessCriteria(required_phrases=["Done"]))
    click_cp = ClickAction(id="c", locator=Locator(strategy="role_name", role="button", name="X"), checkpoint=cp)
    assert hints.is_hint_worthy(click_cp) is True                # checkpoint-bearing -> worthy


def test_simple_actions_not_hint_worthy():
    click = ClickAction(id="c", locator=Locator(strategy="role_name", role="button", name="X"))
    tt = TypeTextAction(id="t", locator=Locator(strategy="css_id", css="#f"), value="v")
    rt = ReadTextAction(id="r", locator=Locator(strategy="css_id", css="#b"))
    assert not hints.is_hint_worthy(click)
    assert not hints.is_hint_worthy(tt)
    assert not hints.is_hint_worthy(rt)


# ---------------- item 3: reverse-parameterization applied to hints ----------------

def test_attach_hints_reverse_parameterizes_session_value():
    art = _artifact(_login_and_lookup_steps())
    # the LLM (hypothetically) leaked a literal account number into the hint text
    raw = {"step_03": "Open activity page for account 19560 to read its balance"}
    out = hints.attach_hints(art, raw, {"account_id": "19560"})
    s3 = next(s for s in out.steps if s.id == "step_03")
    assert s3.metadata is not None
    assert s3.metadata.escalation_hint == "Open activity page for account {{account_id}} to read its balance"
    assert "19560" not in s3.metadata.escalation_hint          # session value scrubbed


def test_attach_hints_only_to_hint_worthy_steps():
    art = _artifact(_login_and_lookup_steps())
    # provide hints for a hint-worthy step AND a non-hint-worthy one; only the worthy one is attached
    raw = {"step_03": "Open the account activity page", "step_02": "Click the login button"}
    out = hints.attach_hints(art, raw, {})
    by_id = {s.id: s for s in out.steps}
    assert by_id["step_03"].metadata.escalation_hint == "Open the account activity page"
    assert by_id["step_02"].metadata is None                   # non-worthy step got no hint even though offered


def test_attach_hints_ignores_unknown_step_ids():
    art = _artifact(_login_and_lookup_steps())
    out = hints.attach_hints(art, {"step_99": "nonexistent"}, {})
    assert all(s.metadata is None for s in out.steps)


# ---------------- backward compat: no hints -> artifact unchanged ----------------

def test_attach_hints_empty_is_noop():
    art = _artifact(_login_and_lookup_steps())
    out = hints.attach_hints(art, {}, {"account_id": "19560"})
    assert out is art                                          # identity: unchanged, backward compatible
    assert all(s.metadata is None for s in out.steps)


# ---------------- item 2: generate_step_hints (mock client) — bounded prompt, only hint-worthy steps ----------------

async def test_generate_step_hints_sends_only_hint_worthy_and_parses_json():
    art = _artifact(_login_and_lookup_steps())
    reply = SimpleNamespace(
        content=[SimpleNamespace(type="text",
                                 text=json.dumps({"step_03": "Open the checking account's activity page"}))],
        usage=SimpleNamespace(input_tokens=120, output_tokens=20,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0))
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=reply)))

    raw, usage = await hints.generate_step_hints(client, "claude-x", art, "look up balance", "http://t/")

    assert raw == {"step_03": "Open the checking account's activity page"}
    # bounded prompt: the user message must NOT contain observation history; it lists ONLY hint-worthy step ids
    call = client.messages.create.await_args
    user_content = call.kwargs["messages"][0]["content"]
    assert "step_03" in user_content
    assert "step_01_0" not in user_content and "step_02" not in user_content   # non-worthy steps excluded
    assert "max_tokens" in call.kwargs and call.kwargs["max_tokens"] <= 600     # bounded output
    assert "tools" not in call.kwargs                                          # lightweight: no tool schema
    assert usage.output_tokens == 20


async def test_generate_step_hints_llm_failure_is_best_effort():
    art = _artifact(_login_and_lookup_steps())
    client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("boom"))))
    raw, usage = await hints.generate_step_hints(client, "claude-x", art, "g", "http://t/")
    assert raw == {} and usage is None                        # never fails discovery over hints


async def test_generate_step_hints_no_worthy_steps_skips_call():
    # an artifact with only simple steps makes NO LLM call (cost guard)
    art = _artifact([ClickAction(id="c", locator=Locator(strategy="role_name", role="button", name="X"))])
    create = AsyncMock()
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    raw, usage = await hints.generate_step_hints(client, "claude-x", art, "g", "http://t/")
    assert raw == {} and usage is None
    create.assert_not_called()


def test_parse_hints_tolerates_code_fence():
    assert hints._parse_hints('```json\n{"a": "x"}\n```') == {"a": "x"}
    assert hints._parse_hints("not json at all") == {}


# ---------------- item 4: StepMetadata model round-trips (present + None) ----------------

def test_step_metadata_field_roundtrips():
    cp = Checkpoint(success=SuccessCriteria(required_phrases=["Done"]))
    with_hint = NavigateAction(id="n", url="x", checkpoint=cp,
                               metadata=StepMetadata(escalation_hint="Do the thing"))
    assert with_hint.metadata.escalation_hint == "Do the thing"
    dumped = with_hint.model_dump()
    assert dumped["metadata"]["escalation_hint"] == "Do the thing"

    without = NavigateAction(id="n", url="x")
    assert without.metadata is None
    # exclude_none drops the empty metadata (backward-compatible YAML)
    assert "metadata" not in without.model_dump(exclude_none=True)
