"""Load a SafetyPolicy from a JSON file, falling back to the in-code default (§3.4 "configurable allowlist").

The policy stays an in-code value object (`policy.py`); this adds the ability to OVERRIDE it from a small JSON
file, so onboarding a second app/tenant is a config edit rather than a Python edit. Reading one file is not
scaling infrastructure (§7 anti-scaling) — there is no loader framework or schema registry, just `json.load`
with a fail-safe fallback.

File shape (`safety_policy.example.json`):

    {"allowed_domains": ["parabank.parasoft.com"],
     "allowed_actions": ["click", "type_text", "navigate", "read_text", "find_matching", "human_input"]}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .parabank_allowlist import PARABANK_POLICY
from .policy import SafetyPolicy

_log = logging.getLogger("safety.policy")

_ROOT = Path(__file__).resolve().parent.parent.parent          # src/safety/ -> repo root
DEFAULT_POLICY_PATH = _ROOT / "safety_policy.json"


def load_policy(path: "Path | str | None" = None) -> SafetyPolicy:
    """Return a `SafetyPolicy` from `path` (default: repo-root `safety_policy.json`).

    If the file is absent, unreadable, or malformed, fall back to the in-code `PARABANK_POLICY` (logged).
    Fail-safe by design: a broken or missing config must NEVER silently widen the allowlist — it collapses to
    the known-narrow default instead.
    """
    p = Path(path) if path is not None else DEFAULT_POLICY_PATH
    if not p.exists():
        return PARABANK_POLICY
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        domains = data["allowed_domains"]
        actions = data["allowed_actions"]
        if not (isinstance(domains, list) and domains and isinstance(actions, list) and actions):
            raise ValueError("allowed_domains and allowed_actions must both be non-empty lists")
        policy = SafetyPolicy(allowed_domains=frozenset(domains), allowed_actions=frozenset(actions))
        _log.info("loaded SafetyPolicy from %s (%d domain(s), %d action(s))",
                  p, len(policy.allowed_domains), len(policy.allowed_actions))
        return policy
    except Exception as exc:  # noqa: BLE001 - any parse/shape error falls back to the safe in-code default
        _log.warning("could not load SafetyPolicy from %s (%s); using the in-code default", p, exc)
        return PARABANK_POLICY
