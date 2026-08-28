"""Ordered step actions (ADR-006 tool set; schema-draft §5).

Runtime actions form a discriminated union on `action`: click, type_text, navigate, read_text,
find_matching. `finish` is a DISCOVERY-TIME tool that produces artifact metadata (success phrases ->
checkpoints, result -> captures) — it is NOT a runtime step and never appears in `Artifact.steps`
(schema-draft §5); it is modeled separately as `FinishAction`.

find_matching iterates `candidates` (a string[] capture) and captures the first candidate whose `probe`
checkpoint passes (`value_from: candidate`). Its `probe` MUST carry a checkpoint (ADR-006).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import Field

from .base import StrictModel
from .checkpoint import Checkpoint
from .locator import Locator


class StepMetadata(StrictModel):
    """Optional per-step metadata (ADR-007 revision). Currently carries `escalation_hint`: a short,
    operator-facing, one-sentence description of what this step is doing, shown in the takeover panel when a
    human intervenes. Authored post-hoc at emission time via a secondary LLM call (NOT by the goal-driven
    discovery loop, which never sees it — §3.1). Reverse-parameterized so it holds no session-specific values.
    Typed (not an untyped dict) to keep the strict schema; `None` when the step has no hint (backward compat)."""
    escalation_hint: Optional[str] = None


class StepBase(StrictModel):
    id: str
    checkpoint: Optional[Checkpoint] = None   # a step may assert a checkpoint after acting (ADR-005 Gap #1)
    metadata: Optional[StepMetadata] = None   # ADR-007 revision: operator hint for the takeover panel


class ClickAction(StepBase):
    action: Literal["click"] = "click"
    locator: Locator


class TypeTextAction(StepBase):
    action: Literal["type_text"] = "type_text"
    locator: Locator
    value: str                                # supports {{name}} interpolation


class NavigateAction(StepBase):
    action: Literal["navigate"] = "navigate"
    url: str                                  # supports {{name}} interpolation


class ReadTextAction(StepBase):
    action: Literal["read_text"] = "read_text"
    locator: Locator


class Probe(StrictModel):
    """Per-candidate action + checkpoint for find_matching (ADR-006). `checkpoint` is REQUIRED."""
    action: str
    locator: Locator
    checkpoint: Checkpoint


class FindMatchingCapture(StrictModel):
    variable: str
    value_from: Literal["candidate"] = "candidate"   # only supported form (schema-draft §5)
    export: Optional[bool] = None
    returned_as: Optional[str] = None


class FindMatchingAction(StepBase):
    action: Literal["find_matching"] = "find_matching"
    candidates: str                            # name of a string[] capture
    probe: Probe
    capture: FindMatchingCapture


class HumanInputAction(StepBase):
    """Planned human intervention (ADR-007 planned mode). At replay this step pauses and shows the overlay in
    PLANNED mode (prompt + single Done button); the human acts directly on the page, clicks Done, and replay
    re-observes and continues. No value is returned to the system (the human interacts with the page itself);
    `prompt` is shown VERBATIM to the human and is never reverse-parameterized. `timeout_ms` hard-caps the
    wait — a timeout becomes a replay hard_failure."""
    action: Literal["human_input"] = "human_input"
    prompt: str
    reason: str
    timeout_ms: int = Field(default=60000, gt=0)


class FinishAction(StrictModel):
    """Discovery-time finish tool signature (ADR-005 + ADR-006). NOT a runtime step; kept out of the Step
    union so it can never appear in `Artifact.steps`."""
    action: Literal["finish"] = "finish"
    result: dict[str, Any]
    success_observed_phrases: list[str]


# Discriminated union of the runtime actions (finish excluded by design).
Step = Annotated[
    Union[ClickAction, TypeTextAction, NavigateAction, ReadTextAction, FindMatchingAction, HumanInputAction],
    Field(discriminator="action"),
]
