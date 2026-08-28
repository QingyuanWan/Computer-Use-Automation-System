"""Typed results, the variable scope, and executor error types.

These are lightweight typed containers (dataclasses) returned to whatever module calls the executor
(normally src/replay/). Kept intentionally simple per the task — no heavy context object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union


@dataclass
class VariableScope:
    """Interpolation source for `{{name}}` substitution: parameters ∪ captures (schema-draft §4).

    NOT frozen: `find_matching` binds a discovered value by writing into `captures` (this is the mechanism
    by which a discovered value propagates to later steps — the caller/replay shares this scope). The
    reserved `candidate` name lives only in a *derived* scope during a find_matching probe and never leaks
    back to the caller's scope (see `derive`).
    """

    parameters: dict[str, Any] = field(default_factory=dict)
    captures: dict[str, Any] = field(default_factory=dict)

    def resolve(self, name: str) -> Any:
        if name in self.captures:
            return self.captures[name]
        if name in self.parameters:
            return self.parameters[name]
        raise KeyError(name)

    def derive(self, **extra: Any) -> "VariableScope":
        """A transient scope with additional names (e.g. the find_matching `candidate`). Uses a *copy* of
        captures so the extra names do not leak into the original scope."""
        caps = dict(self.captures)
        caps.update(extra)
        return VariableScope(parameters=self.parameters, captures=caps)


@dataclass(frozen=True)
class ActionResult:
    status: str                              # "success" | "find_matching_exhausted"
    action: str
    detail: Optional[str] = None
    resulting_url: Optional[str] = None
    matched_count: Optional[int] = None
    locator_strategy: Optional[str] = None
    text: Optional[str] = None               # read_text payload
    value: Optional[str] = None              # type_text value applied
    bound_variable: Optional[str] = None     # find_matching: capture name bound
    bound_value: Optional[str] = None        # find_matching: matched candidate
    candidates_tried: Optional[int] = None
    screenshot_path: Optional[str] = None


@dataclass(frozen=True)
class CheckpointResult:
    status: str                              # "success" | "business_outcome" | "checkpoint_timeout"
    outcome_name: Optional[str] = None       # set when status == "business_outcome"
    observed_text: Optional[str] = None
    screenshot_path: Optional[str] = None    # set when status == "checkpoint_timeout"
    elapsed_ms: Optional[int] = None
    polls: Optional[int] = None


ExecutorResult = Union[ActionResult, CheckpointResult]


class ExecutorError(Exception):
    """Base executor error. May carry an evidence `screenshot_path` (ADR-004 evidence clause)."""

    def __init__(self, message: str, screenshot_path: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.screenshot_path = screenshot_path


class LocatorResolutionError(ExecutorError):
    """Raised when every locator attempt (primary + all fallbacks) matched 0 elements."""

    def __init__(self, message: str, aria: Optional[str] = None, screenshot_path: Optional[str] = None) -> None:
        super().__init__(message, screenshot_path=screenshot_path)
        self.aria = aria


class LocatorAmbiguityError(ExecutorError):
    """Raised when a locator attempt matched N>1 elements. We refuse to auto-pick (silent-failure category)."""

    def __init__(self, message: str, count: int, strategy: Optional[str] = None,
                 screenshot_path: Optional[str] = None) -> None:
        super().__init__(message, screenshot_path=screenshot_path)
        self.count = count
        self.strategy = strategy


class InterpolationError(ExecutorError):
    """Raised when a `{{name}}` token cannot be resolved from the variable scope."""

    def __init__(self, name: str, field: str, template: str) -> None:
        super().__init__(f"interpolation '{{{{{name}}}}}' in field '{field}' does not resolve "
                         f"(template={template!r})")
        self.name = name
        self.field = field
        self.template = template
