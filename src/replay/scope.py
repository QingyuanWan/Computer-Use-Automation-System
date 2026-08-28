"""Variable scope for replay.

We REUSE `src.executor.VariableScope` rather than defining a second class: `PlaywrightExecutor.execute_action`
calls `scope.resolve()` / `scope.derive()` / mutates `scope.captures`, so the scope handed to the executor
MUST be exactly that type. Defining a competing container here would break executor calls. This module adds
the small read/write helpers the replay layer talks in terms of (get / bind_capture) plus a constructor.
"""
from __future__ import annotations

from typing import Any

from src.executor import VariableScope  # single source of truth (executor-compatible API)

__all__ = ["VariableScope", "new_scope", "get", "bind_capture"]


def new_scope(parameters: dict[str, Any]) -> VariableScope:
    """Fresh scope: parameters seeded, captures empty (bound during step execution)."""
    return VariableScope(parameters=dict(parameters), captures={})


def get(scope: VariableScope, name: str) -> Any:
    """Read a name (captures win over parameters; ADR-006 forbids shadowing at model construction)."""
    return scope.resolve(name)


def bind_capture(scope: VariableScope, name: str, value: Any) -> None:
    """Bind a discovered value into the (mutable) captures. Parameters are immutable after init."""
    scope.captures[name] = value
