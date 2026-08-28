"""Unit tests for scripts/run_capability.py (the evaluator-friendly launcher) + scripts/credentials.py.

Covers: auto-reseed keyword detection (login vs non-login technical errors — the secondary net), mode
detection (--artifact-name → replay, --goal → discover), JSON credential round-trip / env bridge / legacy
migration, sample_invocation resolution, the --help + --list-capabilities listing, and the phaseB
escalation-hint roundtrip through Pydantic.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import credentials
from scripts.run_capability import (
    build_parser,
    caller_params_from_sample_invocation,
    is_login_failure,
    list_bundled_artifacts,
    mode_for,
)
from src.replay.results import ReplayResult
from src.storage import ArtifactStorage


# ---------------- auto-reseed keyword detection ----------------

@pytest.mark.parametrize("detail", [
    "capture 'account_type' pattern 'CHECKING' not found in 'An internal error has occurred and has been logged'",
    "The username and password could not be verified",
    "authentication failed for user",
    "capture 'balance' not found in 'Could not find account # 18894'",
    "please log in to continue",
])
def test_is_login_failure_true_on_login_symptoms(detail):
    assert is_login_failure(ReplayResult.technical_error(detail)) is True


@pytest.mark.parametrize("detail", [
    "Playwright TimeoutError: waiting for selector '#amount'",
    "Target page, context or browser has been closed",
    "net::ERR_CONNECTION_REFUSED navigating to activity.htm",
    "capture binding raised for an unrelated reason",
])
def test_is_login_failure_false_on_unrelated_technical_errors(detail):
    assert is_login_failure(ReplayResult.technical_error(detail)) is False


def test_is_login_failure_false_on_non_hard_failures():
    assert is_login_failure(ReplayResult.success({"balance": "$1.00"})) is False
    assert is_login_failure(ReplayResult.business_outcome("account_not_found")) is False
    assert is_login_failure(ReplayResult.stub_unavailable()) is False        # not a technical_error
    assert is_login_failure(ReplayResult.human_aborted()) is False


# ---------------- mode detection ----------------

def test_mode_replay_when_only_artifact_name():
    args = build_parser().parse_args(["--artifact-name", "lookup_checking_balance"])
    assert mode_for(args) == "replay"


def test_mode_replay_when_artifact_file():
    args = build_parser().parse_args(["--artifact-file", "x.yaml"])
    assert mode_for(args) == "replay"


def test_mode_discover_when_goal_present():
    args = build_parser().parse_args(["--goal", "look up balance", "--capability-name", "c"])
    assert mode_for(args) == "discover"


# ---------------- sample_invocation resolution ($json refs → JSON registry) ----------------

_CREDS = {"primary": {"username": "itfai_abc", "password": "pw", "checking_id": "14454", "savings_id": "14455"}}


def test_caller_params_from_sample_invocation_resolves_json():
    art = SimpleNamespace(metadata=SimpleNamespace(sample_invocation={
        "username": "$json:primary.username",
        "account_id": "$json:primary.checking_id",
        "literal_param": "plain_value"}))
    params = caller_params_from_sample_invocation(art, _CREDS)
    assert params == {"username": "itfai_abc", "account_id": "14454", "literal_param": "plain_value"}


def test_caller_params_from_empty_sample_invocation():
    art = SimpleNamespace(metadata=SimpleNamespace(sample_invocation=None))
    assert caller_params_from_sample_invocation(art, _CREDS) == {}


def test_caller_params_from_sample_invocation_missing_path_raises():
    art = SimpleNamespace(metadata=SimpleNamespace(sample_invocation={"x": "$json:primary.nope"}))
    with pytest.raises(credentials.CredentialError):
        caller_params_from_sample_invocation(art, _CREDS)


# ---------------- pre-flight credential verification (_verify_credentials, Option C) ----------------

import scripts.run_capability as rc  # noqa: E402

_LIVE = {"url": "http://t/", "primary": {"username": "itfai_live", "password": "p",
                                         "checking_id": "9", "savings_id": "8"}}
_FRESH = {"url": "http://t/", "primary": {"username": "itfai_fresh", "password": "p",
                                          "checking_id": "111", "savings_id": "222"}}


def _patch_preflight(monkeypatch, *, load, login, reseed):
    monkeypatch.setattr(rc, "ensure_credentials", load)
    monkeypatch.setattr(rc, "login_ok", login)
    monkeypatch.setattr(rc, "do_reseed", reseed)


# contract 1: fresh creds → pre-flight passes silently, no reseed
async def test_preflight_fresh_credentials_proceeds(monkeypatch):
    calls = {"reseed": 0}
    async def _reseed(): calls["reseed"] += 1; return _FRESH
    _patch_preflight(monkeypatch, load=AsyncMock(return_value=_LIVE),
                     login=AsyncMock(return_value=True), reseed=_reseed)
    creds = await rc._verify_credentials()
    assert creds["primary"]["username"] == "itfai_live" and calls["reseed"] == 0


# contract 2: stale creds → detect → reseed → verified fresh account returned
async def test_preflight_stale_reseeds_and_succeeds(monkeypatch):
    states = iter([False, True])          # stale, then the fresh account authenticates
    async def _login(_c): return next(states)
    _patch_preflight(monkeypatch, load=AsyncMock(return_value=_LIVE),
                     login=_login, reseed=AsyncMock(return_value=_FRESH))
    creds = await rc._verify_credentials()
    assert creds["primary"]["username"] == "itfai_fresh"


# contract 3: reseed itself fails → clean PreflightError
async def test_preflight_reseed_failure_clean_error(monkeypatch):
    async def _reseed(): raise RuntimeError("ParaBank registration failed")
    _patch_preflight(monkeypatch, load=AsyncMock(return_value=_LIVE),
                     login=AsyncMock(return_value=False), reseed=_reseed)
    with pytest.raises(rc.PreflightError) as e:
        await rc._verify_credentials()
    assert "reseed failed" in str(e.value).lower()


# contract 4: reseed OK but the fresh account still can't authenticate (ParaBank down) → clean PreflightError
async def test_preflight_fresh_account_still_fails_errors(monkeypatch):
    _patch_preflight(monkeypatch, load=AsyncMock(return_value=_LIVE),
                     login=AsyncMock(return_value=False), reseed=AsyncMock(return_value=_FRESH))
    with pytest.raises(rc.PreflightError) as e:
        await rc._verify_credentials()
    assert "cannot authenticate" in str(e.value).lower() or "login endpoint" in str(e.value).lower()


# contract 5: non-auth failure (login check can't run → login_ok True) → proceed, NO false reseed
async def test_preflight_nonauth_failure_no_reseed(monkeypatch):
    calls = {"reseed": 0}
    async def _reseed(): calls["reseed"] += 1; return _FRESH
    _patch_preflight(monkeypatch, load=AsyncMock(return_value=_LIVE),
                     login=AsyncMock(return_value=True), reseed=_reseed)   # True = check couldn't run
    creds = await rc._verify_credentials()
    assert calls["reseed"] == 0 and creds["primary"]["username"] == "itfai_live"


# ---------------- JSON credential storage (Fix 3) ----------------

_SEED = {
    "PARABANK_PRIMARY_USERNAME": "itfai_abc123",
    # Arbitrary non-functional fixture value (the '#' 500s on ParaBank's login endpoint, so it is not a live
    # secret); the seed script generates a fresh letters+digits password at runtime.
    "PARABANK_PRIMARY_PASSWORD": "Reg1stry#Pw2026",
    "PARABANK_PRIMARY_CHECKING_ID": "12345",
    "PARABANK_PRIMARY_SAVINGS_ID": "12346",
}


def test_credentials_json_roundtrip(tmp_path):
    path = tmp_path / "parabank_credentials.json"
    creds = credentials.from_seed_dict(_SEED)
    assert creds["primary"]["username"] == "itfai_abc123"
    assert creds["primary"]["checking_id"] == "12345"
    assert creds["invalid_account_id"] == "999999999"
    credentials.save_credentials(creds, path=path)
    loaded = credentials.load_credentials(path=path)
    assert loaded == creds                                # exact round-trip


def test_load_credentials_missing_returns_none(tmp_path):
    assert credentials.load_credentials(path=tmp_path / "nope.json") is None


def test_load_credentials_empty_example_treated_as_none(tmp_path):
    # the committed .example (blank username) must read as "no credentials yet" so first-run seeding fires
    path = tmp_path / "creds.json"
    credentials.save_credentials(credentials.from_seed_dict(
        {**_SEED, "PARABANK_PRIMARY_USERNAME": ""}), path=path)
    assert credentials.load_credentials(path=path) is None


# ---------------- listing (--help / --list-capabilities) ----------------

def test_list_bundled_artifacts_reads_metadata():
    listing = list_bundled_artifacts(Path("artifacts"))
    assert "lookup_checking_balance" in listing
    assert "read" in listing


def test_list_bundled_artifacts_missing_dir(tmp_path):
    assert "none" in list_bundled_artifacts(tmp_path / "nope").lower()


def test_list_capabilities_flag_parsed_and_exits(capsys):
    import scripts.run_capability as rc
    args = build_parser().parse_args(["--list-capabilities"])
    assert args.list_capabilities is True
    rc_main_rc = rc.main(["--list-capabilities"])          # must exit 0 without launching a browser
    assert rc_main_rc == 0
    out = capsys.readouterr().out
    assert "lookup_checking_balance" in out


# ---------------- --show-accounts (diagnostic) ----------------

def test_show_accounts_flag_parsed():
    args = build_parser().parse_args(["--show-accounts"])
    assert args.show_accounts is True


def test_show_accounts_dispatched_from_main(monkeypatch):
    """main() must route --show-accounts to _show_accounts (not into replay/discover arg validation)."""
    import scripts.run_capability as rc
    called = {}

    async def _fake_show(args):
        called["ran"] = True
        return 0

    monkeypatch.setattr(rc, "_show_accounts", _fake_show)
    assert rc.main(["--show-accounts"]) == 0
    assert called.get("ran") is True


def test_print_accounts_table_formats_rows(capsys):
    import scripts.run_capability as rc
    creds = {"primary": {"username": "itfai_demo"}}
    rc._print_accounts_table(creds, [
        {"id": "15786", "type": "CHECKING", "balance": "$415.50", "available": "$415.50"},
        {"id": "15897", "type": "SAVINGS", "balance": "$100.00", "available": "$100.00"},
    ])
    out = capsys.readouterr().out
    assert "itfai_demo" in out and "CHECKING" in out and "15897" in out and "$100.00" in out


# ---------------- phaseB escalation-hint roundtrip ----------------

def test_phaseb_artifact_hint_roundtrips_without_double_prefix():
    _fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    art = ArtifactStorage(_fixtures).load_from_path(_fixtures / "phaseB_escalation_test.yaml")
    step = next(s for s in art.steps if s.id == "step_03_nav")
    assert step.metadata is not None
    hint = step.metadata.escalation_hint
    assert hint and "activity page" in hint
    # the panel prepends "About this step: " — the stored hint must NOT already contain that prefix
    assert not hint.lower().startswith("about this step")
