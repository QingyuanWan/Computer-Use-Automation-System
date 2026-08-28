"""Root artifact model + cross-field validators (schema-draft §1, §2, §10).

Cross-field rules enforced here (need whole-artifact visibility):
  - capture names unique (ADR-006)
  - capture names cannot shadow parameter names (ADR-006)
  - every `{{name}}` in a step field resolves to a parameter or capture — plus the reserved `candidate`
    inside a find_matching.probe (schema-draft §11 item 15)  [ADR-006]
  - every capture.source_step references a declared step id (ADR-006). NOTE: the full Gap #4b rule (an
    *exported* finish field must map to a real read, else it is dropped) is an emission-stage concern;
    at the model level we enforce only that source_step names an existing step.
  - find_matching.candidates references a declared capture (ADR-006)
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import Field, model_validator

from .base import StrictModel
from .captures import Capture
from .checkpoint import Checkpoint
from .enums import CapabilityType
from .locator import Locator
from .parameters import ParametersBlock
from .steps import (
    ClickAction,
    FindMatchingAction,
    NavigateAction,
    ReadTextAction,
    Step,
    TypeTextAction,
)

_INTERP = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def _locator_strings(loc: Locator) -> list[str]:
    out: list[str] = []
    for v in (loc.role, loc.name, loc.href_pattern, loc.css, loc.id, loc.text,
              loc.label, loc.placeholder, loc.url_template):
        if v:
            out.append(v)
    for fb in loc.fallbacks:
        out.extend(_locator_strings(fb))
    return out


def _checkpoint_strings(cp: Checkpoint) -> list[str]:
    out = list(cp.success.required_phrases)
    for eo in cp.expected_outcomes:
        out.extend(eo.required_phrases)
    return out


def _assert_resolvable(text: str, allowed: set[str], step_id: str) -> None:
    for token in _INTERP.findall(text):
        base = token.split(".")[0]
        if base not in allowed:
            raise ValueError(
                f"step '{step_id}': interpolation '{{{{{token}}}}}' does not resolve to a declared "
                f"parameter or capture (ADR-006)"
            )


class ArtifactMetadata(StrictModel):
    capability_name: str
    capability_type: CapabilityType
    # How the validation gate obtains caller_parameters (ADR-9). Each value is either a JSON registry reference
    # "$json:dot.path" (re-resolved against test_data/parabank_credentials.json at replay, so live registry
    # changes are picked up) or a plain literal. Required-if-parameters-exist; None for self-contained caps.
    sample_invocation: Optional[dict[str, str]] = None
    validated: bool = False
    # why the auto-replay validation gate did not run (e.g. "requires_human_input" — ADR-007 planned mode).
    validation_skip_reason: Optional[str] = None
    created_at: Optional[str] = None
    discovered_by_model: Optional[str] = None
    target_app_hint: Optional[str] = None
    # Slice 1d: True only when a declared `read` was state-verified at discovery (an injected fingerprint showed
    # no observable target-state change across the run). None = not checked (no provider) or not applicable
    # (mutating). An unverified read declaration therefore does NOT look like a verified one. Additive/optional
    # so older artifacts load unchanged (schema backward-compat).
    state_verified: Optional[bool] = None


class Artifact(StrictModel):
    version: str
    metadata: ArtifactMetadata
    parameters: Optional[ParametersBlock] = None
    captures: list[Capture] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)

    def _param_names(self) -> set[str]:
        return set(self.parameters.properties.keys()) if self.parameters else set()

    def _capture_names(self) -> set[str]:
        return {c.name for c in self.captures}

    def _step_ids(self) -> set[str]:
        return {s.id for s in self.steps}

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> "Artifact":
        params = self._param_names()
        cap_list = [c.name for c in self.captures]

        # capture names unique
        dupes = sorted({n for n in cap_list if cap_list.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate capture names: {dupes} (ADR-006)")
        cap_set = set(cap_list)

        # captures cannot shadow parameters
        shadow = sorted(cap_set & params)
        if shadow:
            raise ValueError(f"capture names shadow parameter names: {shadow} (ADR-006)")

        names = params | cap_set
        step_ids = self._step_ids()

        # capture.source_step must reference an existing step (model-level half of Gap #4b)
        for c in self.captures:
            if c.source_step not in step_ids:
                raise ValueError(
                    f"capture '{c.name}' source_step '{c.source_step}' is not a declared step id "
                    f"(ADR-006 Gap #4b)"
                )

        # interpolation reachability, per step
        for step in self.steps:
            if isinstance(step, (ClickAction, TypeTextAction, ReadTextAction)):
                for s in _locator_strings(step.locator):
                    _assert_resolvable(s, names, step.id)
            if isinstance(step, TypeTextAction):
                _assert_resolvable(step.value, names, step.id)
            if isinstance(step, NavigateAction):
                _assert_resolvable(step.url, names, step.id)
            if step.checkpoint:
                for s in _checkpoint_strings(step.checkpoint):
                    _assert_resolvable(s, names, step.id)
            if isinstance(step, FindMatchingAction):
                if step.candidates not in cap_set:
                    raise ValueError(
                        f"find_matching '{step.id}' candidates '{step.candidates}' is not a declared "
                        f"capture (ADR-006)"
                    )
                probe_allowed = names | {"candidate"}   # reserved name, probe scope only (§11 item 15)
                for s in _locator_strings(step.probe.locator):
                    _assert_resolvable(s, probe_allowed, step.id)
                for s in _checkpoint_strings(step.probe.checkpoint):
                    _assert_resolvable(s, probe_allowed, step.id)
        return self
