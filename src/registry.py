"""ParaBank test-data credential registry (JSON) — the SINGLE credential source of truth.

`.env` holds ONLY `ANTHROPIC_API_KEY`. All ParaBank test-account data lives in
`test_data/parabank_credentials.json` (gitignored) and is referenced from artifacts via
`$json:<dot.path>` values in `metadata.sample_invocation`, resolved here at validation/replay time.

There is NO env-var path for ParaBank credentials — the earlier `$env:`/`--caller-params-from-env`
bridge was removed (single-source refactor; see docs/env_removal_review.md + the ADR-8 revision). This
module lives in `src/` (not `scripts/`) so both `src/` code (CLI, validation gate) and `scripts/` code
(launcher, seed) can import it without a `src/ -> scripts/` layering inversion.

These are throwaway TEST users on a public demo sandbox — fixture data, NOT secrets (§3.4/§9 both allow
this); artifacts store only the *reference* (`$json:...`), never a literal credential value.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent          # src/ -> repo root
CRED_PATH = _ROOT / "test_data" / "parabank_credentials.json"
DEFAULT_URL = "https://parabank.parasoft.com/parabank"
DEFAULT_INVALID_ACCOUNT_ID = "999999999"

# A sample_invocation value of the form "$json:dot.path" is a REGISTRY REFERENCE resolved against the JSON
# store at validation/replay time; any other value is a literal. The path is dotted identifiers only, so a
# stray value like "$json:not a path" (with a space) is safely treated as a literal.
_JSON_REF = re.compile(r"^\$json:([A-Za-z0-9_.]+)$")

_SEED_HELP = "run:  python scripts/seed_parabank_accounts.py"


class CredentialError(RuntimeError):
    """The JSON credential store is missing / blank / corrupt, or a requested dot-path is absent."""


def from_seed_dict(seed: dict, *, url: str = DEFAULT_URL,
                   invalid: str = DEFAULT_INVALID_ACCOUNT_ID) -> dict:
    """Map the seed script's `PARABANK_PRIMARY_*` dict into the JSON credential structure."""
    return {
        "url": seed.get("PARABANK_URL", url),
        "primary": {
            "username": seed["PARABANK_PRIMARY_USERNAME"],
            "password": seed["PARABANK_PRIMARY_PASSWORD"],
            "checking_id": seed["PARABANK_PRIMARY_CHECKING_ID"],
            "savings_id": seed.get("PARABANK_PRIMARY_SAVINGS_ID", ""),
        },
        "invalid_account_id": seed.get("PARABANK_INVALID_ACCOUNT_ID", invalid),
    }


def save_credentials(creds: dict, path: Path = CRED_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")
    return path


def load_credentials(path: Path = CRED_PATH) -> Optional[dict]:
    """Return the credential dict, or None if the file is absent / unreadable / blank (blank username =
    the committed `.example` shape, which must read as 'no credentials yet' so first-run seeding fires)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("primary", {}).get("username"):
        return None
    return data


def require_credentials(path: Path = CRED_PATH) -> dict:
    """load_credentials or raise CredentialError with a seed-pointing message. Fail-fast, NO env fallback."""
    creds = load_credentials(path)
    if creds is None:
        raise CredentialError(f"{CRED_PATH.name} is missing, blank, or corrupt — {_SEED_HELP}")
    return creds


def resolve_ref(dotpath: str, creds: dict) -> str:
    """Resolve a dot-path (e.g. 'primary.checking_id') against `creds` to a scalar string. Raise
    CredentialError if the path is absent or points at a non-scalar."""
    node: object = creds
    for part in dotpath.split("."):
        if not isinstance(node, dict) or part not in node:
            raise CredentialError(f"credential path '{dotpath}' not found in {CRED_PATH.name} — {_SEED_HELP}")
        node = node[part]
    if isinstance(node, (dict, list)):
        raise CredentialError(f"credential path '{dotpath}' does not point to a value in {CRED_PATH.name}")
    return str(node)


def resolve_sample_invocation(sample_invocation: "dict[str, str] | None",
                              creds: "dict | None" = None):
    """Resolve `$json:dot.path` references in a sample_invocation to current literals. Returns
    (resolved, missing) where `missing` lists dot-paths referenced but absent from the store (or absent
    because the store itself could not load). A plain literal passes through unchanged."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    si = sample_invocation or {}
    if creds is None and any(_JSON_REF.match(str(v)) for v in si.values()):
        creds = load_credentials()
    for name, value in si.items():
        m = _JSON_REF.match(str(value))
        if m:
            try:
                resolved[name] = resolve_ref(m.group(1), creds if creds is not None else {})
            except CredentialError:
                missing.append(m.group(1))
        else:
            resolved[name] = value
    return resolved, missing
