"""Unit tests for the emission pipeline (pure functions — no LLM, no Playwright).

Covers behavior-contract items 4 (capability_type prologue strip), 5 (reverse-param guardrails),
6 (untraceable finish fields), 7 (multi-page per-step checkpoints), plus the spec's named edge cases.
"""
from __future__ import annotations

import re

import pytest

from src.agent.agent import _assign_observation_windows
from src.agent.emission import (
    ReadCapabilityMutatedError,
    _generalize_capture_pattern,
    apply_human_input_override,
    assert_read_did_not_mutate,
    build_captures,
    build_checkpoints,
    emit_artifact,
    reverse_parameterize,
    reverse_parameterize_action,
    trace_to_step,
)
from src.agent.results import RecordedStep
from src.agent.tools import TOOLS
from src.models import (
    CapabilityType,
    Checkpoint,
    ClickAction,
    FindMatchingAction,
    FindMatchingCapture,
    Locator,
    NavigateAction,
    Probe,
    ReadTextAction,
    SuccessCriteria,
    TypeTextAction,
)
from src.models import HumanInputAction


def _tt(value="x"):
    return TypeTextAction(id="t", locator=Locator(strategy="css_id", css="#f"), value=value)


def _click(name):
    return ClickAction(id="c", locator=Locator(strategy="role_name", role="button", name=name))


def _nav():
    return NavigateAction(id="n", url="overview.htm")


def _read():
    return ReadTextAction(id="r", locator=Locator(strategy="css_id", css="#b"))


# ---------------- Slice 1: capability_type is caller-declared; only human_input overrides it ----------------

def test_override_passes_declaration_through_without_human_input():
    # No inference anymore — a form-based flow that types post-login is NOT auto-mutating; the declaration
    # stands. (This is exactly the search-box-vs-transfer-box case the old inference got wrong.)
    actions = [_tt("user"), _tt("pass"), _click("Log In"), _tt("999999"), _click("Find")]
    assert apply_human_input_override(CapabilityType.read, actions) == CapabilityType.read
    assert apply_human_input_override(CapabilityType.mutating, actions) == CapabilityType.mutating


def test_override_forces_mutating_on_human_input():
    actions = [HumanInputAction(id="h", prompt="p", reason="r"), _nav(), _read()]
    assert apply_human_input_override(CapabilityType.read, actions) == CapabilityType.mutating
    assert apply_human_input_override(CapabilityType.mutating, actions) == CapabilityType.mutating


# ---------------- Slice 1d: state-delta refusal (pure check, both directions) ----------------

def test_assert_read_did_not_mutate_refuses_on_delta():
    # read + changed fingerprint => refuse (the refusal case is the one that matters)
    with pytest.raises(ReadCapabilityMutatedError):
        assert_read_did_not_mutate(CapabilityType.read, ("checking", "$415.50"), ("checking", "$414.50"))


def test_assert_read_did_not_mutate_passes_when_unchanged():
    assert_read_did_not_mutate(CapabilityType.read, ("checking", "$415.50"), ("checking", "$415.50"))  # no raise


def test_assert_read_did_not_mutate_ignores_mutating_declaration():
    # a mutating capability is out of scope for the read-only check (the consent gate handles it), delta or not
    assert_read_did_not_mutate(CapabilityType.mutating, ("checking", "$415.50"), ("checking", "$414.50"))


# ---------------- item 5: reverse-parameterization guardrails ----------------
# NOTE: the guardrail is "skip values under 3 characters" (contract item 5 + ADR-5 Gap #2), which is why
# these use >=3-char values for substitution and a 2-char value for the skip case. (The Part-1 spec's
# illustrative "25" value is <3 and would be skipped — see the escalation note in the report.)

def test_reverse_param_longest_value_first():
    out = reverse_parameterize("paid $250.00 incl $250 tax", {"amount": "250", "total": "250.00"})
    assert out == "paid ${{total}} incl ${{amount}} tax"


def test_reverse_param_skips_short_values():
    out = reverse_parameterize("pay 42 of 12345", {"x": "42", "acct": "12345"})
    assert out == "pay 42 of {{acct}}"     # "42" (<3 chars) left literal (would hardcode, per ADR-5)


def test_reverse_param_respects_token_boundary():
    out = reverse_parameterize("route 2500 and amount 250", {"amt": "250"})
    assert out == "route 2500 and amount {{amt}}"   # 250 does not match inside 2500


# ---------------- item 6: source-step tracing + untraceable finish fields ----------------

def test_trace_to_step_earliest():
    steps = [RecordedStep("s1", _nav(), observation_after="page one 13899"),
             RecordedStep("s2", _nav(), observation_after="page two 13899")]
    assert trace_to_step("13899", steps) == "s1"       # earliest wins
    assert trace_to_step("nope", steps) is None


def test_build_captures_traceable_and_untraceable():
    steps = [RecordedStep("s1", _read(), observation_after="Account 13899 — Balance: $415.50",
                          read_text_value="$415.50")]
    captures, dropped, warnings = build_captures({"balance": "$415.50", "computed": "$999.99"}, steps)
    by_name = {c.name: c for c in captures}
    assert "balance" in by_name and by_name["balance"].export is True
    assert by_name["balance"].source is None            # read_text-bound (value == read text)
    assert "computed" in dropped and "computed" not in by_name
    assert any("computed" in w for w in warnings)


# ---------------- item 7: multi-page checkpoint attachment ----------------

def test_multi_page_checkpoints_attach_per_step():
    steps = [RecordedStep("s1", _click("Transfer"), observation_after="Transfer Complete! moved funds"),
             RecordedStep("s2", _nav(), observation_after="Overview 13899 $390.50 and 14010 $125.00")]
    by_step, warnings = build_checkpoints(["Transfer Complete!", "$390.50"], steps, {})
    assert by_step == {"s1": ["Transfer Complete!"], "s2": ["$390.50"]}


# ---------------- emit_artifact: full composition + model validation ----------------

def test_emit_artifact_composes_and_validates():
    steps = [
        RecordedStep("s1", _nav(), observation_after="Accounts Overview: account 13899 shown"),
        RecordedStep("s2", _read(), observation_after="Account 13899 — Balance: $415.50",
                     read_text_value="$415.50"),
    ]
    artifact, dropped, warnings = emit_artifact(
        "lookup_balance", steps,
        finish_result={"account_id": "13899", "balance": "$415.50"},
        success_observed_phrases=["Balance: $415.50"],
        model="claude-sonnet-4-6", capability_type=CapabilityType.read, target_app_hint="parabank")

    assert artifact.metadata.validated is False
    assert artifact.metadata.capability_type == CapabilityType.read     # no type_text
    assert dropped == []
    names = {c.name for c in artifact.captures}
    assert names == {"account_id", "balance"}
    # checkpoint attached to s2 (where "$415.50" was observed), reverse-parameterized to {{balance}}
    s2 = artifact.steps[1]
    assert s2.checkpoint is not None
    assert s2.checkpoint.success.required_phrases == ["Balance: {{balance}}"]
    assert artifact.steps[0].checkpoint is None


# ================= FIX A: prompt guidance on phrase selection =================

def test_finish_tool_description_steers_toward_stable_labels():
    finish = next(t for t in TOOLS if t["name"] == "finish")
    desc = finish["input_schema"]["properties"]["success_observed_phrases"]["description"].lower()
    assert "required" in desc
    assert "stable" in desc and "replay" in desc
    assert "per-session" in desc or "account number" in desc   # explicitly warns off dynamic values


def test_emission_handles_mixed_stable_and_dynamic_phrases():
    # a stable label stays literal; a dynamic value that is a capture gets reverse-parameterized; both attach.
    steps = [RecordedStep("s1", _read(), observation_after="Account Details — Balance: $515.50",
                          read_text_value="$515.50")]
    art, dropped, warnings = emit_artifact("cap", steps, {"balance": "$515.50"},
                                           ["Account Details", "Balance: $515.50"], model="m", capability_type=CapabilityType.read)
    phrases = art.steps[0].checkpoint.success.required_phrases
    assert "Account Details" in phrases            # stable label kept literal
    assert "Balance: {{balance}}" in phrases        # dynamic value parameterized (came from a capture)


# ================= FIX B: source-step tracing via observation WINDOW =================

def test_observation_windows_capture_later_and_finish_observations():
    # recorded steps at turns 2 and 5; finish at turn 7. A value that renders late (turn 4) must land in the
    # turn-2 step's window; a value that appears ONLY in the finish observation (turn 7) must land in the
    # last step's window.  observations[t-1] == turn t.
    steps = [RecordedStep("sA", _nav(), turn=2), RecordedStep("sB", _nav(), turn=5)]
    observations = [f"obs{t}" for t in range(1, 8)]
    observations[3] = "obs4 LATE_RENDER"     # turn 4 (inside sA's window: turns 3,4,5)
    observations[6] = "obs7 FINISH_ONLY"     # turn 7 (finish; inside sB's window: turns 6,7)
    _assign_observation_windows(steps, observations, finish_turn=7)
    assert "LATE_RENDER" in steps[0].observation_after
    assert "obs3" in steps[0].observation_after and "obs5" in steps[0].observation_after
    assert "FINISH_ONLY" in steps[1].observation_after   # orphaned-finish-observation bug is fixed
    assert "obs6" in steps[1].observation_after


def test_build_captures_traces_value_present_only_in_window():
    # regression for the integration bug: balance/account_type appeared in a window observation but were
    # dropped before the window fix. With the window as observation_after they now trace + export.
    steps = [RecordedStep("s1", _nav(), observation_after="Accounts Overview, no detail yet"),
             RecordedStep("s2", _read(),
                          observation_after="Account Type: CHECKING\nBalance: $515.50\nAccount Number: 26220")]
    captures, dropped, warnings = build_captures(
        {"account_type": "CHECKING", "balance": "$515.50", "account_number": "26220"}, steps)
    assert {c.name for c in captures} == {"account_type", "balance", "account_number"}
    assert dropped == []


# ================= FIX C: untraceable phrases are DROPPED, not best-effort-attached =================

def test_build_checkpoints_drops_untraceable_phrase():
    steps = [RecordedStep("s1", _click("X"), observation_after="Welcome Ada — your account is ready")]
    by_step, warnings = build_checkpoints(["Welcome", "PHANTOM PHRASE NEVER OBSERVED"], steps, {})
    assert by_step == {"s1": ["Welcome"]}                         # traceable attached
    assert all("PHANTOM" not in p for ps in by_step.values() for p in ps)   # untraceable NOT attached
    assert any("PHANTOM" in w and "DROPPED" in w for w in warnings)


def test_build_checkpoints_warns_when_no_phrase_traceable():
    steps = [RecordedStep("s1", _click("X"), observation_after="totally unrelated page")]
    by_step, warnings = build_checkpoints(["NOPE ONE", "NOPE TWO"], steps, {})
    assert by_step == {}
    assert any("NO checkpoint" in w for w in warnings)


# ================= ADR-9: caller_parameters -> step-field reverse-parameterization =================

def test_rp_action_navigate_url():
    act = NavigateAction(id="n", url="activity.htm?id=12345")
    new, used = reverse_parameterize_action(act, {"account_id": "12345"})
    assert new.url == "activity.htm?id={{account_id}}"
    assert used == {"account_id"}


def test_rp_action_type_text_value_and_locator_name():
    act = TypeTextAction(id="t", locator=Locator(strategy="role_name", role="textbox", name="12345"),
                         value="transfer to 12345")
    new, used = reverse_parameterize_action(act, {"acct": "12345"})
    assert new.value == "transfer to {{acct}}"
    assert new.locator.name == "{{acct}}"
    assert new.locator.role == "textbox"          # role is NOT parameterized
    assert used == {"acct"}


def test_rp_action_nested_findmatching_probe_locator():
    fm = FindMatchingAction(
        id="f", candidates="accts",
        probe=Probe(action="navigate",
                    locator=Locator(strategy="href_pattern", href_pattern="activity.htm?id=12345"),
                    checkpoint=Checkpoint(success=SuccessCriteria(required_phrases=["Balance"]))),
        capture=FindMatchingCapture(variable="acct"))
    new, used = reverse_parameterize_action(fm, {"account_id": "12345"})
    assert new.probe.locator.href_pattern == "activity.htm?id={{account_id}}"
    assert used == {"account_id"}


def test_rp_action_locator_fallbacks_recurse():
    act = ClickAction(id="c", locator=Locator(strategy="role_name", role="link", name="Home",
                      fallbacks=[Locator(strategy="href_pattern", href_pattern="x?id=12345")]))
    new, used = reverse_parameterize_action(act, {"account_id": "12345"})
    assert new.locator.fallbacks[0].href_pattern == "x?id={{account_id}}"
    assert used == {"account_id"}


def test_emit_declares_parameters_and_records_sample_invocation():
    steps = [RecordedStep("s1", NavigateAction(id="n", url="activity.htm?id=12345"),
                          observation_after="Account Details — Balance: $415.50"),
             RecordedStep("s2", _read(), observation_after="Account Details — Balance: $415.50",
                          read_text_value="$415.50")]
    art, dropped, warnings = emit_artifact("lookup_balance", steps, {"balance": "$415.50"},
                                           ["Account Details"], model="m", capability_type=CapabilityType.read,
                                           caller_parameters={"account_id": "12345"})
    assert art.metadata.sample_invocation == {"account_id": "12345"}
    assert art.parameters is not None and "account_id" in art.parameters.properties
    assert art.parameters.properties["account_id"].type == "string"     # integer-looking id -> string
    assert art.parameters.required == ["account_id"]
    assert art.steps[0].url == "activity.htm?id={{account_id}}"          # step field templated
    assert art.metadata.capability_type == CapabilityType.read


def test_emit_without_caller_params_leaves_sample_invocation_none():
    steps = [RecordedStep("s1", _read(), observation_after="Balance: $415.50", read_text_value="$415.50")]
    art, _, _ = emit_artifact("cap", steps, {"balance": "$415.50"}, ["Balance"], model="m", capability_type=CapabilityType.read)
    assert art.metadata.sample_invocation is None
    assert art.parameters is None                 # backward-compatible no-op


def test_emit_drops_input_echo_finish_field():
    # a finish field that merely echoes a caller parameter (by value) is dropped from captures (ADR-9)
    steps = [RecordedStep("s1", NavigateAction(id="n", url="activity.htm?id=12345"),
                          observation_after="Account 12345 — Balance: $415.50"),
             RecordedStep("s2", _read(), observation_after="Account 12345 — Balance: $415.50",
                          read_text_value="$415.50")]
    art, dropped, warnings = emit_artifact("lookup", steps,
                                           {"account_id": "12345", "balance": "$415.50"},
                                           ["Account Details"], model="m", capability_type=CapabilityType.read,
                                           caller_parameters={"account_id": "12345"})
    names = {c.name for c in art.captures}
    assert "account_id" not in names and "account_id" in dropped   # echo dropped, not exported
    assert "balance" in names                                       # genuine output kept
    assert any("echo" in w for w in warnings)


def test_param_type_inference():
    steps = [RecordedStep("s1", NavigateAction(id="n", url="pay?amt=25.50&acct=12345"),
                          observation_after="ok")]
    art, _, _ = emit_artifact("cap", steps, {}, [], model="m", capability_type=CapabilityType.read,
                              caller_parameters={"amount": "25.50", "account_id": "12345", "flag": "true"})
    props = art.parameters.properties
    assert props["amount"].type == "number"       # decimal -> number
    assert props["account_id"].type == "string"   # integer id -> string (schema §3 Example A)
    assert props["flag"].type == "boolean"


# ================= Cross-tenant fix: capture-pattern generalization (currency/date/number) =================

def test_generalize_currency():
    # a session-specific balance is generalized to the currency shape (NOT hardcoded to \$415\.50)
    assert _generalize_capture_pattern("$415.50") == r"\$[\d,]+\.\d{2}"


def test_generalize_currency_with_comma():
    assert _generalize_capture_pattern("$1,234.56") == r"\$[\d,]+\.\d{2}"


def test_generalize_currency_large():
    # adversarial: a very large balance still matches the currency shape
    assert _generalize_capture_pattern("$1,234,567.89") == r"\$[\d,]+\.\d{2}"


def test_generalize_date_dash():
    assert _generalize_capture_pattern("08-20-2026") == r"\d{2}[-/]\d{2}[-/]\d{4}"


def test_generalize_date_slash():
    # MM/DD/YYYY with slashes matches the 2-2-4 library shape
    assert _generalize_capture_pattern("08/20/2026") == r"\d{2}[-/]\d{2}[-/]\d{4}"


def test_iso_date_year_first_not_matched():
    # BOUND (faithful to the mandated library): \d{2}[-/]\d{2}[-/]\d{4} is 2-2-4, so an ISO YYYY/MM/DD date
    # (4-2-2) is NOT a date match. "2026/08/20" also isn't a bare number -> kept as an exact escaped literal.
    # The library is closed to the 3 approved shapes; ISO dates would require extending it (forbidden here).
    assert _generalize_capture_pattern("2026/08/20") == re.escape("2026/08/20")


def test_generalize_number():
    assert _generalize_capture_pattern("12345") == r"\d+"


def test_non_matching_stable_label_kept_hardcoded():
    # CHECKING (a stable enum-like value) is NOT a dynamic shape -> exact escaped literal preserved
    assert _generalize_capture_pattern("CHECKING") == "CHECKING"


def test_non_matching_free_text_kept_hardcoded_and_escaped():
    # free text with regex metacharacters is preserved AND escaped (exact-match behavior, e.g. error codes)
    value = "Error #42 (a.b+c)"
    out = _generalize_capture_pattern(value)
    import re as _re
    assert out == _re.escape(value)
    # the escaped pattern must match the original literal exactly
    assert _re.fullmatch(out, value)


def test_non_matching_currency_no_dollar_sign_kept_hardcoded():
    # adversarial: "415.50" without a $ sign is NOT the currency shape and not a bare integer -> exact literal
    assert _generalize_capture_pattern("415.50") == r"415\.50"


def test_empty_string_edge_case():
    # empty value matches no shape -> re.escape("") == "" (existing behavior preserved)
    assert _generalize_capture_pattern("") == ""


def test_generalize_wired_into_build_captures():
    # end-to-end through build_captures: a source-extract balance capture gets the generalized pattern,
    # while a stable account_type keeps its exact literal.
    steps = [RecordedStep("s1", _nav(),
                          observation_after="Account Type: CHECKING\nBalance: $415.50")]
    captures, dropped, warnings = build_captures(
        {"account_type": "CHECKING", "balance": "$415.50"}, steps)
    by_name = {c.name: c for c in captures}
    assert by_name["balance"].source.extract.pattern == r"\$[\d,]+\.\d{2}"   # generalized (cross-tenant safe)
    assert by_name["account_type"].source.extract.pattern == "CHECKING"      # stable label kept exact


def test_generalized_pattern_matches_a_different_tenant_value():
    # the core cross-tenant guarantee: the generalized balance pattern matches tenant B's DIFFERENT balance
    import re as _re
    pattern = _generalize_capture_pattern("$415.50")   # discovered at tenant A
    assert _re.search(pattern, "Balance: $425.50")     # replays against tenant B ($425.50)
    assert _re.search(pattern, "Balance: $1,234,567.89")
