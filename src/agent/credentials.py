"""Credential detection for emission (ADR-010).

Field-name-based detection ONLY (α strategy, user decision Q1): a field is credential-bearing when a NAME
associated with it — a locator's name/label/placeholder/id/text, or a caller-parameter name — matches a
conservative pattern. This is used to (a) mark credential caller-parameters `sensitive: true` and (b) B2
auto-parameterize a hardcoded credential typed into a *named* field. NO positional or value-shape heuristic
is ever used to auto-parameterize (they mis-classify non-credential fields — e.g. an account id vs a
username, exactly the false positive Q1 rules out).

`looks_like_credential_value` is a SEPARATE value-shape heuristic used ONLY by the B1 refuse-to-emit safety
net — never to parameterize. B1 only ever *refuses*; a false positive there is a loud, safe over-refusal, not
a silently mis-named parameter.

Deliberate deviation from the brief's field list: "account" is NOT a username pattern here — it collides with
the legitimate non-credential `account_id` parameter and would produce exactly the misclassification Q1
forbids.
"""
from __future__ import annotations

import re
from typing import Optional

# (canonical parameter name, case-insensitive name regex). Order matters: password before the generic
# username arm. Word-ish anchors keep 'account_id'/'member_id' from matching.
_CREDENTIAL_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("password", re.compile(r"pass(word|wd)?|\bpwd\b", re.I)),
    ("username", re.compile(r"user[\s_-]?name|user[\s_-]?id|\buser\b|\blogin\b|log[\s_-]?in|sign[\s_-]?in|signin", re.I)),
    ("email",    re.compile(r"\be[-\s]?mail\b", re.I)),
    ("ssn",      re.compile(r"\bssn\b|social[\s_-]?security", re.I)),
    ("pin",      re.compile(r"\bpin\b", re.I)),
    ("otp",      re.compile(r"\botp\b|one[\s_-]?time[\s_-]?(code|password|pin)", re.I)),
    ("token",    re.compile(r"\btoken\b", re.I)),
    ("api_key",  re.compile(r"api[\s_-]?key", re.I)),
    ("secret",   re.compile(r"\bsecret\b", re.I)),
    ("credential", re.compile(r"credential", re.I)),
]

_MIN_CRED_LEN = 6


class CredentialsHardcodedError(Exception):
    """B1 safety net: emission found a step still holding a literal that LOOKS like a credential but was not
    auto-parameterized (its field carried no credential-matching name), so the system refuses to persist a
    possible secret into the artifact (§3.4 / §9 / ADR-010). The message names the suspect steps (values are
    redacted) and tells the operator how to fix it."""


def detect_credential(*name_fields: Optional[str]) -> Optional[str]:
    """Return the canonical credential parameter name for the FIRST name-field that matches a credential
    pattern, else None. Field-name (α) matching only — never inspects the value."""
    for raw in name_fields:
        if not raw:
            continue
        for canonical, rx in _CREDENTIAL_PATTERNS:
            if rx.search(str(raw)):
                return canonical
    return None


def looks_like_credential_value(value: Optional[str]) -> bool:
    """B1-ONLY value-shape heuristic (never used to parameterize): a hardcoded literal that plausibly is a
    secret — length >= 6 AND contains BOTH a letter and a digit. Conservative enough to skip names ('John'),
    amounts ('100.00') and pure-numeric ids ('19560'), while catching mixed tokens ('itfai_3824e30a',
    'Reg1stry#Pw2026'). Never treats a {{placeholder}} as a credential."""
    v = str(value or "")
    if len(v) < _MIN_CRED_LEN or "{{" in v:
        return False
    return any(c.isalpha() for c in v) and any(c.isdigit() for c in v)
