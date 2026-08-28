"""`generate` marker synthesis (ADR-006 + schema-draft §3 OQ-5 resolution).

Synthesized ONCE per replay invocation, before step execution. Uniqueness scope is per-run:
`<base>_<YYYYMMDDHHMMSS>_<4-hex>` (schema-draft §3). `base` defaults to the parameter name.
"""
from __future__ import annotations

import datetime
import secrets

from src.models import Artifact, GenerateMarker


def _unique_string(base: str) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{base}_{ts}_{secrets.token_hex(2)}"   # 4 hex chars


def generate_value(marker: GenerateMarker, base: str) -> str:
    if marker == GenerateMarker.unique_string:
        return _unique_string(base)
    if marker == GenerateMarker.unique_email:
        return f"{_unique_string(base)}@example.test"
    if marker == GenerateMarker.timestamp:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    raise ValueError(f"unknown generate marker: {marker!r}")


def synthesize(artifact: Artifact) -> dict[str, str]:
    """Return {param_name: generated_value} for every parameter carrying a generate marker."""
    out: dict[str, str] = {}
    if artifact.parameters is None:
        return out
    for name, param in artifact.parameters.properties.items():
        if param.generate is not None:
            out[name] = generate_value(param.generate, base=name)
    return out
