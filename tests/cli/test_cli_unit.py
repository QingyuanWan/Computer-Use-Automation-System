"""CLI unit tests (contract 11) — pure wiring/arg logic, no browser/LLM. The full end-to-end is the paid
verification run."""
from __future__ import annotations

import pytest

import src.cli as cli
from src.cli import _build_parser, _caller_param_sources, _origin, _resolve_caller_params

_SAMPLE_CREDS = {
    "url": "https://parabank.parasoft.com/parabank",
    "primary": {"username": "itfai_test", "password": "pw", "checking_id": "15564", "savings_id": "15675"},
    "invalid_account_id": "999999999",
}


# ---- --caller-params-from-json resolves against the JSON registry (the ONLY credential source) --------

def test_resolve_caller_params_from_json(monkeypatch):
    monkeypatch.setattr(cli.registry, "require_credentials", lambda *a, **k: _SAMPLE_CREDS)
    assert _resolve_caller_params(["username=primary.username", "account_id=primary.checking_id"]) == {
        "username": "itfai_test", "account_id": "15564"}


def test_resolve_caller_params_empty_no_store_access(monkeypatch):
    # empty pairs → {} and must NOT touch the store (fail-fast only when creds are actually needed)
    def _boom(*a, **k):
        raise AssertionError("require_credentials must not be called for empty pairs")
    monkeypatch.setattr(cli.registry, "require_credentials", _boom)
    assert _resolve_caller_params([]) == {}
    assert _resolve_caller_params(None) == {}


def test_resolve_caller_params_missing_store_fails_fast(monkeypatch):
    def _raise(*a, **k):
        raise cli.registry.CredentialError("parabank_credentials.json is missing")
    monkeypatch.setattr(cli.registry, "require_credentials", _raise)
    with pytest.raises(SystemExit):
        _resolve_caller_params(["account_id=primary.checking_id"])


def test_resolve_caller_params_missing_path_fails_fast(monkeypatch):
    monkeypatch.setattr(cli.registry, "require_credentials", lambda *a, **k: _SAMPLE_CREDS)
    with pytest.raises(SystemExit):
        _resolve_caller_params(["account_id=primary.nonexistent"])


def test_resolve_caller_params_malformed_fails_fast(monkeypatch):
    monkeypatch.setattr(cli.registry, "require_credentials", lambda *a, **k: _SAMPLE_CREDS)
    with pytest.raises(SystemExit):
        _resolve_caller_params(["not_a_pair"])


def test_caller_param_sources_builds_json_refs():
    assert _caller_param_sources(["account_id=primary.checking_id"]) == \
        {"account_id": "$json:primary.checking_id"}
    assert _caller_param_sources([]) == {}
    assert _caller_param_sources(None) == {}


def test_env_flag_is_removed():
    """--caller-params-from-env must be genuinely gone (argparse rejects it)."""
    with pytest.raises(SystemExit):
        _build_parser().parse_args([
            "discover", "--goal", "g", "--target-url", "http://t/", "--capability-name", "c",
            "--caller-params-from-env", "account_id=PARABANK_PRIMARY_CHECKING_ID"])


def test_origin():
    assert _origin("https://parabank.parasoft.com/parabank/index.htm") == "https://parabank.parasoft.com"


def test_parser_discover():
    args = _build_parser().parse_args([
        "discover", "--goal", "g", "--target-url", "http://t/", "--capability-name", "cap",
        "--capability-type", "read",
        "--caller-params-from-json", "account_id=primary.checking_id"])
    assert args.command == "discover" and args.goal == "g" and args.capability_name == "cap"
    assert args.capability_type == "read"
    assert args.caller_params_from_json == ["account_id=primary.checking_id"]
    assert args.headed is False and args.skip_validation is False and args.max_steps == 20


def test_parser_discover_requires_capability_type():
    # Slice 1: --capability-type has no default; omitting it is an argparse error (a safety label is deliberate)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["discover", "--goal", "g", "--target-url", "u", "--capability-name", "c"])


def test_parser_replay():
    args = _build_parser().parse_args(["replay", "--artifact-name", "cap", "--headed"])
    assert args.command == "replay" and args.artifact_name == "cap" and args.headed is True


def test_parser_discover_requires_goal():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["discover", "--target-url", "u", "--capability-name", "c"])


def test_explain_hard_failure_decodes_phase3_subtypes():
    from src.cli import _explain_hard_failure
    assert "stub_unavailable" in _explain_hard_failure("stub_unavailable")
    assert "human_aborted" in _explain_hard_failure("human_aborted")
    out = _explain_hard_failure("technical_error:playwright crashed")
    assert "technical_error" in out and "playwright crashed" in out
    assert "other" in _explain_hard_failure(None)


def test_explain_hard_failure_decodes_escalation_exhausted():
    from src.cli import _explain_hard_failure
    bare = _explain_hard_failure("escalation_exhausted")
    assert "escalation_exhausted" in bare and "technical_error" not in bare
    detailed = _explain_hard_failure("escalation_exhausted:checkpoint_unresolved_after_escalation")
    assert "escalation_exhausted" in detailed and "checkpoint_unresolved_after_escalation" in detailed


def test_explain_hard_failure_decodes_safety_blocked_mutating():
    from src.cli import _explain_hard_failure
    out = _explain_hard_failure("safety_blocked:mutating_requires_consent")
    assert "safety_blocked" in out and "--i-understand-mutating" in out
    generic = _explain_hard_failure("safety_blocked:allowlist_domain")
    assert "safety_blocked" in generic and "allowlist_domain" in generic


def test_parser_replay_i_understand_mutating():
    args = _build_parser().parse_args(["replay", "--artifact-name", "cap", "--i-understand-mutating"])
    assert args.i_understand_mutating is True
    default = _build_parser().parse_args(["replay", "--artifact-name", "cap"])
    assert default.i_understand_mutating is False
