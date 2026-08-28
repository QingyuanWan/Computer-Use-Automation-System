"""InputValidator — capability_type sanity check.

A declared `read` capability must not mutate state. The login prologue (typing credentials + the Log In click)
is legitimate in a read flow, so it is stripped before the check — exactly mirroring the emission classifier —
so `lookup_checking_balance` (read: login + navigate + read_text) does NOT trip, while a `read` capability that
types into a form and submits post-login does. Duck-typed against the artifact model (no `src/` imports).
"""
from __future__ import annotations

import re

from .errors import SafetyViolationError

_LOGIN_CLICK = re.compile(r"log\s*in|login|sign\s*in", re.IGNORECASE)


class InputValidator:
    @staticmethod
    def _post_login_steps(steps):
        """Return the steps AFTER the login prologue — i.e. after the first click whose locator name looks like
        a Log In / Sign In button. If there is no such click, all steps are considered post-login."""
        for i, step in enumerate(steps or []):
            if getattr(step, "action", None) == "click":
                name = getattr(getattr(step, "locator", None), "name", "") or ""
                if _LOGIN_CLICK.search(name):
                    return list(steps)[i + 1:]
        return list(steps or [])

    @staticmethod
    def _capability_type(artifact) -> "str | None":
        meta = getattr(artifact, "metadata", None)
        return getattr(getattr(meta, "capability_type", None), "value",
                       getattr(meta, "capability_type", None))

    def check_capability_type(self, artifact) -> None:
        # A declared `read` capability must not contain a human_input step — a human pause can change state, so
        # it must be `mutating` (emission enforces this override at authoring; this is the replay-time backstop).
        # The old "post-login type_text => mutation" rule was removed (Slice 1): it was a tautology (emission
        # inferred the type with the same rule the validator then checked, so it could never fire) AND it could
        # not tell a search box from a transfer box, so it would wrongly refuse a legitimate form-based read.
        if self._capability_type(artifact) != "read":
            return
        if any(getattr(s, "action", None) == "human_input" for s in getattr(artifact, "steps", [])):
            raise SafetyViolationError(
                "capability_type_mismatch",
                "capability declared 'read' but contains a human_input step (a human pause may change state; "
                "declare it 'mutating')")

    def check_mutating_consent(self, artifact, allow_mutating: bool) -> None:
        """A `mutating` capability changes real target state on EVERY replay (transfer_funds moves $10,
        request_loan opens a loan), so an unattended replay must not run it silently. Block it terminally
        unless the caller passed explicit consent (`--i-understand-mutating`). `read` capabilities are never
        affected. (D1 fix, ADR-008 §Safety.)"""
        if allow_mutating:
            return
        if self._capability_type(artifact) == "mutating":
            raise SafetyViolationError(
                "mutating_requires_consent",
                "capability is 'mutating' (changes real state on every replay); re-run with "
                "--i-understand-mutating to consent")
