"""Unit tests for src/registry.py — the single JSON credential source + $json: dot-path resolver."""
from __future__ import annotations

import pytest

from src import registry

_CREDS = {
    "url": "https://parabank.parasoft.com/parabank",
    "primary": {"username": "itfai_x", "password": "pw", "checking_id": "15564", "savings_id": "15675"},
    "invalid_account_id": "999999999",
}


def _write(path, creds):
    import json
    path.write_text(json.dumps(creds), encoding="utf-8")
    return path


# ---- load / require -------------------------------------------------------------------------------

def test_load_credentials_roundtrip(tmp_path):
    p = registry.save_credentials(_CREDS, path=tmp_path / "c.json")
    assert registry.load_credentials(p) == _CREDS


def test_load_credentials_missing_returns_none(tmp_path):
    assert registry.load_credentials(tmp_path / "nope.json") is None


def test_load_credentials_blank_username_returns_none(tmp_path):
    p = _write(tmp_path / "c.json", {"primary": {"username": ""}})
    assert registry.load_credentials(p) is None


def test_require_credentials_raises_when_missing(tmp_path):
    with pytest.raises(registry.CredentialError):
        registry.require_credentials(tmp_path / "nope.json")


# ---- resolve_ref ----------------------------------------------------------------------------------

def test_resolve_ref_happy():
    assert registry.resolve_ref("primary.checking_id", _CREDS) == "15564"
    assert registry.resolve_ref("url", _CREDS) == _CREDS["url"]


def test_resolve_ref_missing_path_raises():
    with pytest.raises(registry.CredentialError):
        registry.resolve_ref("primary.nonexistent", _CREDS)


def test_resolve_ref_nonscalar_raises():
    with pytest.raises(registry.CredentialError):
        registry.resolve_ref("primary", _CREDS)   # points at a dict, not a value


# ---- resolve_sample_invocation --------------------------------------------------------------------

def test_resolve_sample_invocation_json_and_literal():
    resolved, missing = registry.resolve_sample_invocation(
        {"account_id": "$json:primary.checking_id", "amount": "10"}, creds=_CREDS)
    assert resolved == {"account_id": "15564", "amount": "10"} and missing == []


def test_resolve_sample_invocation_missing():
    resolved, missing = registry.resolve_sample_invocation({"a": "$json:primary.nope"}, creds=_CREDS)
    assert missing == ["primary.nope"] and "a" not in resolved


def test_resolve_sample_invocation_loads_store_when_no_creds(monkeypatch):
    monkeypatch.setattr(registry, "load_credentials", lambda *a, **k: _CREDS)
    resolved, missing = registry.resolve_sample_invocation({"account_id": "$json:primary.checking_id"})
    assert resolved == {"account_id": "15564"} and missing == []


def test_resolve_sample_invocation_none():
    assert registry.resolve_sample_invocation(None) == ({}, [])
