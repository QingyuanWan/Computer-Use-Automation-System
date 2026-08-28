"""Safety module: all 9 behavior-contract items (each a distinct test) + the two wiring proofs + an
end-to-end evil-domain replay. Mocked/duck-typed — no browser, no LLM."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.executor import PlaywrightExecutor, VariableScope
from src.models import Artifact, NavigateAction
from src.models.artifact import ArtifactMetadata
from src.models.enums import CapabilityType
from src.replay import ReplayEngine, StubEscalationHandler
from src.safety import PARABANK_POLICY, PIIRedactor, SafetyGate, SafetyPolicy, SafetyViolationError


def _nav(url):
    return NS(action="navigate", url=url)


# ---------------- contract 1: action → non-allowlist domain ----------------
def test_action_offdomain_blocked():
    with pytest.raises(SafetyViolationError) as e:
        SafetyGate().check_action(_nav("https://evil.example.com/x"))
    assert e.value.rule_name == "allowlist_domain"


def test_action_parabank_domain_and_relative_ok():
    g = SafetyGate()
    g.check_action(_nav("https://parabank.parasoft.com/parabank/activity.htm?id={{account_id}}"))
    g.check_action(_nav("activity.htm?id=1"))          # relative → resolves to allowed base → permitted


# ---------------- contract 2: unauthorized action type ----------------
def test_action_unauthorized_type_blocked():
    with pytest.raises(SafetyViolationError) as e:
        SafetyGate().check_action(NS(action="run_shell"))
    assert e.value.rule_name == "allowlist_action_type"


# ---------------- contract 3: declared read + human_input step is refused (Slice 1 rule) ----------------
def test_capability_type_mismatch_blocked():
    # The rule is now "declared read + human_input => refuse" (a human pause may change state). The old
    # syntactic "post-login type_text => mutation" rule was removed — it wrongly refused form-based reads.
    art = NS(metadata=NS(capability_type="read"),
             steps=[NS(action="type_text", locator=NS(name="")), NS(action="type_text", locator=NS(name="")),
                    NS(action="click", locator=NS(name="Log In")),
                    NS(action="human_input", locator=NS(name=""))])
    with pytest.raises(SafetyViolationError) as e:
        SafetyGate().check_capability(art)
    assert e.value.rule_name == "capability_type_mismatch"


def test_read_with_post_login_type_text_not_flagged():
    # Slice 1 behavior change: a form-based READ (e.g. a transaction search that types criteria post-login) is
    # trusted, NOT auto-refused. This is exactly the case the removed syntactic inference got wrong.
    art = NS(metadata=NS(capability_type="read"),
             steps=[NS(action="type_text", locator=NS(name="")), NS(action="type_text", locator=NS(name="")),
                    NS(action="click", locator=NS(name="Log In")),
                    NS(action="type_text", locator=NS(name="amount")),
                    NS(action="click", locator=NS(name="Find Transactions"))])
    SafetyGate().check_capability(art)                 # must NOT raise


def test_read_login_flow_not_flagged():
    # lookup_checking_balance shape: login type_text is the prologue, then navigate + read_text → legitimate read
    art = NS(metadata=NS(capability_type="read"),
             steps=[NS(action="type_text", locator=NS(name="")), NS(action="type_text", locator=NS(name="")),
                    NS(action="click", locator=NS(name="Log In")),
                    NS(action="navigate", url="https://parabank.parasoft.com/parabank/activity.htm"),
                    NS(action="read_text", locator=NS(name=""))])
    SafetyGate().check_capability(art)                 # must not raise


def test_mutating_capability_type_not_flagged():
    # The capability_type sanity check only applies to `read`; a mutating cap does NOT trip it. (Consent is a
    # separate gate — see the D1 tests below — so here we opt in to isolate the type check.)
    art = NS(metadata=NS(capability_type="mutating"),
             steps=[NS(action="type_text", locator=NS(name="amount")), NS(action="click", locator=NS(name="Transfer"))])
    SafetyGate(allow_mutating=True).check_capability(art)


# ---------------- D1: mutating capability requires explicit consent ----------------
def test_mutating_capability_blocked_without_consent():
    art = NS(metadata=NS(capability_type="mutating"),
             steps=[NS(action="type_text", locator=NS(name="amount")), NS(action="click", locator=NS(name="Transfer"))])
    with pytest.raises(SafetyViolationError) as e:
        SafetyGate().check_capability(art)             # default allow_mutating=False -> refuse
    assert e.value.rule_name == "mutating_requires_consent"


def test_mutating_capability_allowed_with_consent():
    art = NS(metadata=NS(capability_type="mutating"),
             steps=[NS(action="type_text", locator=NS(name="amount")), NS(action="click", locator=NS(name="Transfer"))])
    SafetyGate(allow_mutating=True).check_capability(art)   # explicit consent -> no raise


def test_read_capability_never_needs_consent():
    art = NS(metadata=NS(capability_type="read"),
             steps=[NS(action="click", locator=NS(name="Log In")),
                    NS(action="navigate", url="https://parabank.parasoft.com/parabank/activity.htm"),
                    NS(action="read_text", locator=NS(name=""))])
    SafetyGate().check_capability(art)                 # read: allow_mutating irrelevant, must not raise


async def test_mutating_replay_hard_failure_without_consent():
    # end-to-end via the real engine + real gate (no browser): a mutating artifact replayed without consent
    # short-circuits to hard_failure(safety_blocked:mutating_requires_consent) at pre-flight.
    art = Artifact(
        version="0.1.0",
        metadata=ArtifactMetadata(capability_name="transfer_probe", capability_type=CapabilityType.mutating),
        steps=[NavigateAction(id="go", url="https://parabank.parasoft.com/parabank/transfer.htm")])
    eng = ReplayEngine(executor=MagicMock(), escalation_handler=StubEscalationHandler(),
                       artifact_loader=lambda n: None, safety_gate=SafetyGate())
    res = await eng.replay(art, {})
    assert res.status == "hard_failure" and res.reason == "safety_blocked:mutating_requires_consent"


# ---------------- contract 4: sensitive:true param value redacted ----------------
def _artifact_with_sensitive(**sens):
    props = {name: NS(sensitive=val) for name, val in sens.items()}
    return NS(parameters=NS(properties=props))


def test_sensitive_param_value_redacted():
    art = _artifact_with_sensitive(password=True, account_id=None)
    out = PIIRedactor().redact("logged in with s3cretPW at 12345", art, {"password": "s3cretPW", "account_id": "12345"})
    assert "s3cretPW" not in out and "[REDACTED]" in out


# ---------------- contract 5: field-name fallback (password/ssn/pin) regardless of marker ----------------
def test_field_name_fallback_json_redacted():
    out = PIIRedactor().redact('{"ssn": "111-22-3333", "PIN": "4821"}')       # no artifact, no values
    assert "111-22-3333" not in out and "4821" not in out and out.count("[REDACTED]") == 2


def test_field_name_fallback_by_param_name():
    out = PIIRedactor().redact("pin was 9999", values={"pin": "9999"})        # name matches fallback, no marker
    assert "9999" not in out


# ---------------- contract 6: non-sensitive params (account_id) NOT redacted ----------------
def test_account_id_not_redacted():
    art = _artifact_with_sensitive(account_id=None)
    out = PIIRedactor().redact("account 12345 balance", art, {"account_id": "12345"})
    assert "12345" in out                              # ADR-9 debuggability preserved


# ---------------- contract 7: redactor idempotent ----------------
def test_redactor_idempotent():
    r = PIIRedactor()
    art = _artifact_with_sensitive(password=True)
    once = r.redact('{"password": "hunter2", "value": "hunter2"}', art, {"password": "hunter2"})
    assert r.redact(once, art, {"password": "hunter2"}) == once


# ---------------- contract 8: check_action invoked from the executor dispatch path ----------------
async def test_check_action_invoked_from_executor(monkeypatch, tmp_path):
    from src.executor import executor as exmod
    gate = MagicMock()
    ex = PlaywrightExecutor(evidence_dir=tmp_path, safety_gate=gate)
    ex.page = object()                                  # satisfy _require_started without a real browser
    monkeypatch.setattr(exmod.action_dispatcher, "dispatch", AsyncMock(return_value="dispatched"))
    result = await ex.execute_action(NS(action="click"), VariableScope())
    gate.check_action.assert_called_once()
    assert result == "dispatched"


# ---------------- contract 9: check_capability invoked from replay() ----------------
async def test_check_capability_invoked_from_replay():
    gate = MagicMock()
    gate.check_capability.side_effect = SafetyViolationError("allowlist_domain")
    eng = ReplayEngine(executor=MagicMock(), escalation_handler=StubEscalationHandler(),
                       artifact_loader=lambda n: None, safety_gate=gate)
    res = await eng.replay(NS(metadata=NS(capability_name="x")), {})
    gate.check_capability.assert_called_once()
    assert res.status == "hard_failure" and res.reason == "safety_blocked:allowlist_domain"


# ---------------- end-to-end: evil.example.com artifact → replay hard_failure (real gate, no browser) ----------------
async def test_evil_domain_replay_hard_failure():
    art = Artifact(version="0.1.0",
                   metadata=ArtifactMetadata(capability_name="evil_probe", capability_type=CapabilityType.read),
                   steps=[NavigateAction(id="go", url="https://evil.example.com/steal")])
    eng = ReplayEngine(executor=MagicMock(), escalation_handler=StubEscalationHandler(),
                       artifact_loader=lambda n: None, safety_gate=SafetyGate())
    res = await eng.replay(art, {})
    assert res.status == "hard_failure" and res.reason == "safety_blocked:allowlist_domain"


# ---------------- policy injection: SafetyGate is policy-driven, not hardcoded ----------------
def test_default_policy_is_parabank():
    assert SafetyGate().policy is PARABANK_POLICY
    assert "parabank.parasoft.com" in SafetyGate().policy.allowed_domains


def test_custom_policy_allows_its_own_domain_and_actions():
    # A DIFFERENT injected policy is honored: its domain + action types pass the gate.
    pol = SafetyPolicy(allowed_domains={"internal.bank.example"}, allowed_actions={"navigate", "click"})
    g = SafetyGate(policy=pol)
    g.check_action(_nav("https://internal.bank.example/accounts"))   # on the injected allowlist
    g.check_action(NS(action="click"))                                # sanctioned action type
    with pytest.raises(SafetyViolationError) as e:
        g.check_action(NS(action="read_text"))       # NOT in this policy's actions (though ParaBank allows it)
    assert e.value.rule_name == "allowlist_action_type"


def test_custom_policy_blocks_what_parabank_allows():
    # The same injected policy REFUSES ParaBank's own domain — proving the allow-set is the policy's, not hardcoded.
    pol = SafetyPolicy(allowed_domains={"internal.bank.example"}, allowed_actions={"navigate"})
    with pytest.raises(SafetyViolationError) as e:
        SafetyGate(policy=pol).check_action(_nav("https://parabank.parasoft.com/parabank/index.htm"))
    assert e.value.rule_name == "allowlist_domain"


# ---------------- gate default off (keeps existing replays/tests permissive unless injected) ----------------
def test_permissive_gate_allows_everything():
    g = SafetyGate.permissive()
    g.check_action(_nav("https://evil.example.com/x"))                       # no raise
    g.check_capability(NS(metadata=NS(capability_type="read"),
                          steps=[NS(action="type_text", locator=NS(name="x"))]))   # no raise
