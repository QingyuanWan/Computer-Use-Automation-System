"""Async-tolerant checkpoint polling with three-way branching (ADR-005; schema-draft §7).

Each poll reads the target region's text and evaluates, in order:
  1. all success.required_phrases present (after interpolation)  -> success
  2. any expected_outcome's phrases all present                  -> business_outcome(name)
  3. neither                                                     -> keep polling
On timeout (wait_ms exhausted): capture an evidence screenshot and return checkpoint_timeout.

The checkpoint_timeout -> escalation flow is orchestrated by src/replay/, NOT here — the executor just
returns the timeout honestly.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .evidence import EvidenceCapture
from .interpolation import interpolate
from .results import CheckpointResult, VariableScope

_log = logging.getLogger("executor.checkpoint")


async def _read_target(page, target: str) -> str:
    """Text content of `target` (a selector). Missing element -> '' (so we keep polling, not raise)."""
    if not target:
        target = "body"
    loc = page.locator(target)
    try:
        if await loc.count() >= 1:
            return await loc.first.inner_text()
    except Exception as exc:
        _log.debug("read target %r failed transiently: %s", target, exc)
    return ""


async def resolve_checkpoint(page, checkpoint, scope: VariableScope, evidence: EvidenceCapture,
                             capture_evidence: bool = True, business_only: bool = False) -> CheckpointResult:
    """Poll `checkpoint` against the page. `capture_evidence=False` suppresses the timeout screenshot — used
    for find_matching probe checks, whose timeouts are expected iteration steps, not genuine failures.

    `business_only=True` evaluates ONLY the expected_outcomes (never success) and, crucially, does NOT
    interpolate the success phrases — used by the replay engine when a success CAPTURE could not be produced
    (ADR-005): the success phrases may reference that same failed capture (e.g. `{{account_type}}`), so
    interpolating them would raise; but the business phrases are capture-independent and can still classify
    the page as a recognized business_outcome (schema-draft §10 'no such account is a legitimate answer')."""
    target = checkpoint.success.target
    success_phrases = ([] if business_only else
                       [interpolate(p, scope, "checkpoint.success.required_phrases")
                        for p in checkpoint.success.required_phrases])
    outcomes = [
        (o.name, [interpolate(p, scope, f"expected_outcomes[{o.name}]") for p in o.required_phrases])
        for o in checkpoint.expected_outcomes
    ]

    start = time.monotonic()
    deadline = start + checkpoint.wait_ms / 1000.0
    polls = 0
    text = ""
    while True:
        text = await _read_target(page, target)
        polls += 1
        if success_phrases and all(p in text for p in success_phrases):
            return CheckpointResult(status="success", observed_text=text[:2000], polls=polls,
                                    elapsed_ms=int((time.monotonic() - start) * 1000))
        for name, phrases in outcomes:
            if phrases and all(p in text for p in phrases):
                return CheckpointResult(status="business_outcome", outcome_name=name,
                                        observed_text=text[:2000], polls=polls,
                                        elapsed_ms=int((time.monotonic() - start) * 1000))
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(checkpoint.poll_interval_ms / 1000.0)

    shot = await evidence.capture("checkpoint_timeout") if capture_evidence else None
    return CheckpointResult(status="checkpoint_timeout", observed_text=text[:2000], screenshot_path=shot,
                            polls=polls, elapsed_ms=int((time.monotonic() - start) * 1000))
