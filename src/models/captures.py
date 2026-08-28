"""Runtime-discovered typed values (ADR-006; schema-draft §4).

Each capture is bound by a step (`source_step`) and may `export` into the caller-facing return contract.
`source` is absent for find_matching-bound captures (their value comes from the find_matching step's
`value_from: candidate`, schema-draft §11 item 16). `extract` pulls a substring/attribute from a compound
value (ADR-006 path B); `extract.all` produces a list capture for find_matching.candidates (schema-draft
§11 item 7, provisional). Array `type` is written as the shorthand ``string[]`` (schema-draft §11 item 13).
"""
from __future__ import annotations

from typing import Optional

from pydantic import Field

from .base import StrictModel
from .locator import Locator


class ExtractSpec(StrictModel):
    pattern: str
    from_: str = Field(alias="from")     # ``from`` is a Python keyword -> aliased
    all: bool = False


class CaptureSource(StrictModel):
    locator: Locator
    extract: Optional[ExtractSpec] = None


class Capture(StrictModel):
    name: str
    type: str                            # e.g. "string", "string[]" (array shorthand, §11 item 13)
    source_step: str
    source: Optional[CaptureSource] = None
    export: Optional[bool] = None
    returned_as: Optional[str] = None
