"""Artifact schema models (Pydantic v2). Authoritative spec: docs/schema-draft.md.

Public surface re-exported here so callers (e.g. executor, replay, storage) import from `src.models`
without reaching into submodules.
"""
from __future__ import annotations

from .artifact import Artifact, ArtifactMetadata
from .base import StrictModel
from .captures import Capture, CaptureSource, ExtractSpec
from .checkpoint import Checkpoint, ExpectedOutcome, SuccessCriteria
from .enums import ActionType, CapabilityType, EscalationReason, HumanOutcome
from .escalation import EscalationEvent
from .locator import Locator, LocatorStrategy
from .parameters import GenerateMarker, Parameter, ParametersBlock
from .steps import (
    ClickAction,
    FindMatchingAction,
    FindMatchingCapture,
    FinishAction,
    HumanInputAction,
    NavigateAction,
    Probe,
    ReadTextAction,
    Step,
    StepBase,
    StepMetadata,
    TypeTextAction,
)

__all__ = [
    "StrictModel",
    "Artifact",
    "ArtifactMetadata",
    "ParametersBlock",
    "Parameter",
    "GenerateMarker",
    "Capture",
    "CaptureSource",
    "ExtractSpec",
    "Checkpoint",
    "SuccessCriteria",
    "ExpectedOutcome",
    "Locator",
    "LocatorStrategy",
    "StepBase",
    "StepMetadata",
    "ClickAction",
    "TypeTextAction",
    "NavigateAction",
    "ReadTextAction",
    "FindMatchingAction",
    "HumanInputAction",
    "FindMatchingCapture",
    "Probe",
    "FinishAction",
    "Step",
    "EscalationEvent",
    "CapabilityType",
    "ActionType",
    "EscalationReason",
    "HumanOutcome",
]
