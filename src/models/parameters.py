"""Caller-provided inputs — the typed capability signature (ADR-006; schema-draft §3).

A `parameters` block is a JSON-Schema `object` whose `properties` map names to `Parameter`s. Each Parameter
combines a JSON-Schema type spec with our extensions: `generate` (fresh-value synthesis per run, ADR-006 +
fixture_discovery Q9; uniqueness spec in schema-draft §3), and `export`/`returned_as` (PROVISIONAL
parameter-export, schema-draft §11 item 5 — needed so a fixture can return the username it generated and the
password it used, neither of which is a page-read value). Nested JSON Schema (`items`/`properties`) is held
as an opaque fragment per ADR-008.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import Field, model_validator

from .base import StrictModel


class GenerateMarker(str, Enum):
    unique_string = "unique_string"
    unique_email = "unique_email"
    timestamp = "timestamp"


class Parameter(StrictModel):
    type: str
    generate: Optional[GenerateMarker] = None
    export: Optional[bool] = None
    returned_as: Optional[str] = None
    default: Optional[Any] = None
    description: Optional[str] = None
    enum: Optional[list[Any]] = None
    items: Optional[dict[str, Any]] = None        # JSON Schema fragment (array item schema)
    properties: Optional[dict[str, Any]] = None   # JSON Schema fragment (object properties)
    sensitive: Optional[bool] = None
    """Declare ``sensitive: true`` on parameters carrying credentials (username, password, token). When set,
    the safety redactor (``src/safety/redactor.py``, wired into the discovery evidence writers) substitutes the
    parameter's value with ``[REDACTED]`` in evidence files (``.txt`` / ``.json``), keeping the artifact and its
    evidence safely shareable. Set automatically by the emission credential detector (ADR-010); authors of
    hand-written artifacts should set it on any credential field. Non-sensitive params (e.g. ``account_id``) are
    left intact for debuggability (ADR-9). (schema-draft §3.)"""

    @model_validator(mode="after")
    def _generate_default_exclusive(self) -> "Parameter":
        # schema-draft §11 item 17: a generated value ignores any caller value, so pairing it with a
        # static default is contradictory.
        if self.generate is not None and self.default is not None:
            raise ValueError("a parameter cannot declare both 'generate' and 'default' (schema-draft §11 item 17)")
        return self


class ParametersBlock(StrictModel):
    type: str = "object"
    properties: dict[str, Parameter] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _required_in_properties(self) -> "ParametersBlock":
        missing = [r for r in self.required if r not in self.properties]
        if missing:
            raise ValueError(f"'required' names not present in properties: {missing}")
        return self
