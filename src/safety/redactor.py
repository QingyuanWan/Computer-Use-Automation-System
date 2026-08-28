"""PIIRedactor — parameter-declaration-based redaction of secrets in evidence text.

Declaration-based, NOT regex-content-based (ADR-9): a value is redacted because a parameter was DECLARED
`sensitive: true` (or its name matches a small credential fallback set), never because it "looks like" a
secret — so a legitimate `account_id` is preserved for debuggability. Duck-typed against the artifact model
(reads `.parameters.properties[*].sensitive`); no `src/` imports.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"
# Field-name fallback: values under a dict key (or a parameter named) matching these are redacted even without
# an explicit sensitive marker. Deliberately narrow (ADR-9): does NOT include "account".
_FALLBACK_NAME = re.compile(r"password|ssn|pin", re.IGNORECASE)
# Redacts the value in a JSON pair whose KEY contains password/ssn/pin, e.g.  "ssn": "111-22-3333".
_JSON_SENSITIVE_PAIR = re.compile(r'("\w*(?:password|ssn|pin)\w*"\s*:\s*")([^"]*)(")', re.IGNORECASE)


class PIIRedactor:
    @staticmethod
    def sensitive_param_names(artifact) -> "set[str]":
        props = getattr(getattr(artifact, "parameters", None), "properties", None) or {}
        return {name for name, p in props.items() if getattr(p, "sensitive", None)}

    def redact(self, text: str, artifact=None, values: "dict | None" = None) -> str:
        """Replace secret values with [REDACTED] in `text`. Redacted are: (1) the runtime `values` of
        parameters declared `sensitive: true` in `artifact`; (2) the runtime `values` of any key whose NAME
        matches the credential fallback (password/ssn/pin); (3) JSON `"<...password/ssn/pin...>": "<value>"`
        pairs by field name. Non-sensitive params (e.g. account_id) are left intact. Idempotent."""
        if not text:
            return text
        values = values or {}
        sensitive_names = self.sensitive_param_names(artifact) if artifact is not None else set()
        literals: set[str] = set()
        for name, val in values.items():
            if (name in sensitive_names or _FALLBACK_NAME.search(name)) and val not in (None, ""):
                literals.add(str(val))
        # longest-first so a value that is a substring of another is not partially clobbered
        for lit in sorted(literals, key=len, reverse=True):
            text = text.replace(lit, _REDACTED)
        text = _JSON_SENSITIVE_PAIR.sub(lambda m: m.group(1) + _REDACTED + m.group(3), text)
        return text
