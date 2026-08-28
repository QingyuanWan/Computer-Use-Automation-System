"""Platform-agnostic semantic locator (ADR-002; schema-draft §6).

The Executor translates a Locator into Playwright calls at replay; no Playwright syntax lives in the
artifact. `strategy` is an advisory label — the actual match uses whichever fields are present (schema-draft
§11 item 18). Fields `index`/`label`/`placeholder`/`url_template` are experiment-derived (§11 item 10). Any
string field may contain `{{name}}` interpolation, checked at Artifact construction.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import Field

from .base import StrictModel


class LocatorStrategy(str, Enum):
    role_name = "role_name"
    role_nth = "role_nth"
    href_pattern = "href_pattern"
    css = "css"
    css_id = "css_id"
    id = "id"
    text = "text"
    label = "label"
    placeholder = "placeholder"
    url_template = "url_template"


class Locator(StrictModel):
    strategy: Optional[LocatorStrategy] = None
    role: Optional[str] = None
    name: Optional[str] = None
    href_pattern: Optional[str] = None
    css: Optional[str] = None
    id: Optional[str] = None
    text: Optional[str] = None
    index: Optional[int] = None            # positional nth among matches (0-based)
    label: Optional[str] = None
    placeholder: Optional[str] = None
    url_template: Optional[str] = None      # used by navigate-per-candidate inside find_matching.probe
    fallbacks: list["Locator"] = Field(default_factory=list)   # ordered fallback chain (ADR-002)


Locator.model_rebuild()  # resolve the self-referential `fallbacks: list[Locator]`
