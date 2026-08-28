"""LLM-driven discovery agent (ADR-004/005/006/008).

Part 1: core discovery loop + artifact emission. Part 2: failure-injection sub-run (expected_outcomes for
read capabilities) + auto-replay validation gate. Boundary (ADR-008): depends only on src/models +
src/executor + src/replay + anthropic. This is the only module that calls the LLM.
"""
from __future__ import annotations

from .agent import DiscoveryAgent
from .config import AgentConfigError, DEFAULT_MODEL, load_api_key
from .failure_injection import STRATEGIES, InjectionStrategy, classify_injection, run_failure_injection
from .results import (
    AgentError,
    DiscoveryResult,
    FinishValidationError,
    RecordedStep,
    TranslationError,
)
from .validation_gate import run_validation_gate

__all__ = [
    "DiscoveryAgent",
    "DiscoveryResult",
    "RecordedStep",
    "AgentError",
    "AgentConfigError",
    "FinishValidationError",
    "TranslationError",
    "load_api_key",
    "DEFAULT_MODEL",
    # Part 2
    "run_failure_injection",
    "classify_injection",
    "InjectionStrategy",
    "STRATEGIES",
    "run_validation_gate",
]
