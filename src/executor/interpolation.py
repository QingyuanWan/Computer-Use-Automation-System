"""Forward `{{name}}` interpolation against a VariableScope (schema-draft §4).

Executor only substitutes FORWARD (scope value -> string). Reverse-parameterization (recorded literal ->
`{{name}}`) is an agent-side concern and is deliberately NOT done here. Values are stringified via `str()`;
there are no format specifiers in this scope (schema-draft §4 / §11.14).
"""
from __future__ import annotations

import re
from typing import Optional

from .results import InterpolationError, VariableScope

# `{{ name }}` or `{{ obj.field }}` — the base name (before any dot) is what we resolve.
_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def interpolate(text: Optional[str], scope: VariableScope, field: str = "<unknown>") -> Optional[str]:
    """Substitute every `{{name}}` in `text`. Returns None unchanged. Raises InterpolationError on an
    undefined name (never silently leaves the token in place — that would be a silent-failure category)."""
    if text is None:
        return None

    def _repl(m: "re.Match[str]") -> str:
        token = m.group(1)
        base = token.split(".")[0]
        try:
            return str(scope.resolve(base))
        except KeyError:
            raise InterpolationError(name=base, field=field, template=text)

    return _TOKEN.sub(_repl, text)
