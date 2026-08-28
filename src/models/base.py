"""Shared strict base model for all artifact-derived types.

Strictness is deliberate (schema-draft §Pydantic config): `extra='forbid'` so an unknown/mistyped field is
a hard error rather than silently dropped; `frozen=True` so a constructed artifact is immutable; and
`populate_by_name=True` so fields with an alias (e.g. ExtractSpec.from_ aliased ``from``) can be built by
either name in Python while round-tripping the YAML alias.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        populate_by_name=True,
    )
