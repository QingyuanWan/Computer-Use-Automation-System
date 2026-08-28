"""SafetyPolicy — the injectable allow-set for a target app (ADR-012 revision).

NOT a config file / loader (§7 anti-scaling): this is an in-code, dependency-injected value object. A
`SafetyGate` takes a `SafetyPolicy` at construction time; the default is the ParaBank policy
(`parabank_allowlist.PARABANK_POLICY`). A different target ships its own `SafetyPolicy` of the same shape (or
one is injected in a test) with no change to the gate/checker logic — that is the seam that makes the allowlist
policy-driven rather than hardcoded, while external YAML/JSON policy loading stays deferred (REPORT §7).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyPolicy:
    """The domains and action types a target permits. Frozen + normalized to frozenset so a policy is an
    immutable, hashable value object callers may build from plain `set`/`list` literals."""
    allowed_domains: "frozenset[str]"
    allowed_actions: "frozenset[str]"

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_domains", frozenset(self.allowed_domains))
        object.__setattr__(self, "allowed_actions", frozenset(self.allowed_actions))
