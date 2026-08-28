"""AllowlistChecker — matches an action's URL domain + action type against an allow-set.

The allowed domains + action types come from a `SafetyPolicy` (SafetyGate builds the checker from
`policy.allowed_domains` / `policy.allowed_actions`); the module constants are the ParaBank defaults used when
the checker is constructed directly. Duck-typed against the action/step/artifact models (reads `.action`,
`.url`, `.steps`) so the safety module imports nothing from other `src/` packages (ADR-008 boundary; testable
with SimpleNamespace mocks).
"""
from __future__ import annotations

import urllib.parse

from .errors import SafetyViolationError
from .parabank_allowlist import ALLOWED_ACTION_TYPES, ALLOWED_DOMAINS


class AllowlistChecker:
    def __init__(self, allowed_domains=ALLOWED_DOMAINS, allowed_action_types=ALLOWED_ACTION_TYPES) -> None:
        self.allowed_domains = frozenset(allowed_domains)
        self.allowed_action_types = frozenset(allowed_action_types)

    @staticmethod
    def _domain_of(url: "str | None") -> str:
        return urllib.parse.urlparse(url or "").netloc

    def _check_url(self, url: "str | None") -> None:
        # A relative URL (no netloc, e.g. "activity.htm?id=...") resolves against the allowed base → permitted.
        # `{{account_id}}` templates live in the path/query, never the netloc, so no interpolation is needed.
        domain = self._domain_of(url)
        if domain and domain not in self.allowed_domains:
            raise SafetyViolationError("allowlist_domain", f"domain {domain!r} not on the allowlist")

    def check_action(self, action) -> None:
        """Per-action pre-dispatch check: the action type must be sanctioned, and a navigate's target domain
        must be on the allowlist."""
        kind = getattr(action, "action", None)
        if kind not in self.allowed_action_types:
            raise SafetyViolationError("allowlist_action_type", f"action type {kind!r} is not permitted")
        if kind == "navigate":
            self._check_url(getattr(action, "url", None))

    def check_capability_domains(self, artifact) -> None:
        """Capability pre-flight: every navigate step's absolute URL must be on the allowlist."""
        for step in getattr(artifact, "steps", []) or []:
            if getattr(step, "action", None) == "navigate":
                self._check_url(getattr(step, "url", None))
