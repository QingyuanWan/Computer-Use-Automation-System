"""SafetyGate — composes the checkers and exposes the two production hooks.

- `check_action(action, scope)` runs at the executor's pre-dispatch point (allowlist domain + action type).
- `check_capability(artifact)` runs at replay pre-flight before the step loop (allowlist domain coverage +
  capability_type sanity).

Default construction is the ENFORCING ParaBank gate; `SafetyGate.permissive()` disables all checks (used as
the default in the engine/executor so unit tests are unaffected — production entry points inject an enforcing
gate). A violation raises `SafetyViolationError(rule_name)`, which the replay engine converts to
`hard_failure(reason="safety_blocked:<rule_name>")`.

The allowlist is policy-driven: the gate takes a `SafetyPolicy` (allowed_domains, allowed_actions) at
construction; the default is `PARABANK_POLICY`. A different target injects a different policy — no gate/checker
edits (see policy.py / parabank_allowlist.py).
"""
from __future__ import annotations

from .allowlist import AllowlistChecker
from .parabank_allowlist import PARABANK_POLICY
from .policy import SafetyPolicy
from .validator import InputValidator


class SafetyGate:
    def __init__(self, policy: SafetyPolicy = PARABANK_POLICY, *,
                 allowlist: "AllowlistChecker | None" = None,
                 validator: "InputValidator | None" = None, enforce: bool = True,
                 allow_mutating: bool = False) -> None:
        # Policy-driven allow-set (injectable; default = ParaBank). The checker derives its allowed sets from
        # self.policy, so swapping the policy changes what the gate permits with no code change.
        self.policy = policy
        self.allowlist = allowlist or AllowlistChecker(policy.allowed_domains, policy.allowed_actions)
        self.validator = validator or InputValidator()
        self.enforce = enforce
        # D1 fix: a `mutating` capability is blocked at pre-flight unless the caller opted in (the CLI/launcher
        # flip this true when `--i-understand-mutating` is passed). Default False = safe (block silent mutation).
        self.allow_mutating = allow_mutating

    @classmethod
    def permissive(cls) -> "SafetyGate":
        """A no-op gate: the hooks are still invoked (so wiring is provable) but every check passes."""
        return cls(enforce=False)

    def check_action(self, action, scope=None) -> None:
        if not self.enforce:
            return
        self.allowlist.check_action(action)

    def check_capability(self, artifact) -> None:
        if not self.enforce:
            return
        self.allowlist.check_capability_domains(artifact)
        self.validator.check_capability_type(artifact)
        self.validator.check_mutating_consent(artifact, self.allow_mutating)
