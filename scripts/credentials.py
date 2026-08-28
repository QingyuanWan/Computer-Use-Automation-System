"""Backward-compatible import shim.

The ParaBank credential registry now lives in `src/registry.py` (the single JSON source of truth). This
module re-exports it so existing `from scripts import credentials` / `scripts.credentials.*` call sites keep
working. The env-var bridge (`credentials_to_env`) and the legacy `.env`->JSON migration (`from_legacy_env`)
were REMOVED in the single-source refactor — JSON is the only credential source (docs/env_removal_review.md).
"""
from __future__ import annotations

from src.registry import (  # noqa: F401
    CRED_PATH,
    DEFAULT_INVALID_ACCOUNT_ID,
    DEFAULT_URL,
    CredentialError,
    from_seed_dict,
    load_credentials,
    require_credentials,
    resolve_ref,
    resolve_sample_invocation,
    save_credentials,
)
