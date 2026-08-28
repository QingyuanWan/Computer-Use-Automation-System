"""The default ParaBank SafetyPolicy.

This is the in-code default `SafetyPolicy` injected into a `SafetyGate` when no other policy is supplied — the
domains and action types permitted for the ParaBank target. It is a code constant (§7: no config file / loader),
but the values are now wrapped in an injectable `SafetyPolicy` (see policy.py), so a different target supplies a
different policy at construction time rather than editing this module. External YAML/JSON policy loading for
multi-app deployment is deferred (REPORT §7).
"""
from __future__ import annotations

from .policy import SafetyPolicy

# Domains an action/capability may touch. ParaBank serves everything from this one host.
ALLOWED_DOMAINS = frozenset({"parabank.parasoft.com"})

# The complete, sanctioned action tool set (schema-draft §5). Any action whose type is outside this set is
# unauthorized and blocked before dispatch.
ALLOWED_ACTION_TYPES = frozenset({
    "click", "type_text", "navigate", "read_text", "find_matching", "human_input",
})

# The default policy dependency-injected into SafetyGate.
PARABANK_POLICY = SafetyPolicy(allowed_domains=ALLOWED_DOMAINS, allowed_actions=ALLOWED_ACTION_TYPES)
