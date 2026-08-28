"""Per-action-type execution (ADR-006 tool set; schema-draft §5).

Dispatches on the Pydantic discriminator field `action` (our models use `action`, the generic name the
task calls `.type`). Each branch is kept small; find_matching is factored into its own helper.

Executor boundary: this file only drives Playwright + the resolver/checkpoint helpers. No LLM, no artifact
I/O, no orchestration across steps (that is src/replay/).
"""
from __future__ import annotations

import asyncio
import logging

from .interpolation import interpolate
from .locator_resolver import resolve_locator
from .results import ActionResult, ExecutorError, VariableScope

_log = logging.getLogger("executor.action")


async def dispatch(executor, action, scope: VariableScope) -> ActionResult:
    kind = action.action
    if kind == "click":
        return await _click(executor, action, scope)
    if kind == "type_text":
        return await _type_text(executor, action, scope)
    if kind == "navigate":
        return await _navigate(executor, action, scope)
    if kind == "read_text":
        return await _read_text(executor, action, scope)
    if kind == "find_matching":
        return await _find_matching(executor, action, scope)
    if kind == "human_input":
        return await _human_input(executor, action, scope)
    raise ExecutorError(f"unknown action type: {kind!r}")


async def _human_input(executor, action, scope) -> ActionResult:
    """Planned intervention (ADR-007). Invoke the injected escalation handler in planned mode and block until
    the human clicks Done. Timeout -> status 'human_input_timeout' (the engine turns that into a hard_failure).
    No capture/scope binding — the human acts on the page itself, no value returns."""
    handler = getattr(executor, "escalation_handler", None)
    if handler is None:
        raise ExecutorError("human_input step reached but no escalation_handler is wired to the executor")
    hint = action.metadata.escalation_hint if getattr(action, "metadata", None) else None
    try:
        await handler.escalate_planned(action.prompt, action.reason, timeout_ms=action.timeout_ms, hint=hint)
    except asyncio.TimeoutError:
        return ActionResult(status="human_input_timeout", action="human_input",
                            resulting_url=executor.page.url if executor.page else None)
    return ActionResult(status="success", action="human_input",
                        resulting_url=executor.page.url if executor.page else None)


async def _click(executor, action, scope) -> ActionResult:
    loc = await resolve_locator(executor.page, action.locator, scope, executor.evidence)
    await loc.click()
    return ActionResult(status="success", action="click",
                        locator_strategy=str(action.locator.strategy),
                        resulting_url=executor.page.url)


async def _type_text(executor, action, scope) -> ActionResult:
    loc = await resolve_locator(executor.page, action.locator, scope, executor.evidence)
    value = interpolate(action.value, scope, field="type_text.value")
    # Executor translation (schema-draft §5): typing into a <select> becomes select_option; otherwise fill()
    # (fill clears-then-sets, which is the right semantics for reactive form fields vs. type()).
    tag = await loc.evaluate("el => el.tagName.toLowerCase()")
    if tag == "select":
        try:
            await loc.select_option(value=value)
        except Exception:
            await loc.select_option(label=value)
    else:
        await loc.fill(value)
    return ActionResult(status="success", action="type_text", value=value,
                        locator_strategy=str(action.locator.strategy))


async def _navigate(executor, action, scope) -> ActionResult:
    url = interpolate(action.url, scope, field="navigate.url")
    await executor.page.goto(url)
    return ActionResult(status="success", action="navigate", resulting_url=executor.page.url)


async def _read_text(executor, action, scope) -> ActionResult:
    loc = await resolve_locator(executor.page, action.locator, scope, executor.evidence)
    text = await loc.inner_text()
    return ActionResult(status="success", action="read_text", text=text,
                        locator_strategy=str(action.locator.strategy))


async def _execute_probe(executor, probe, temp_scope) -> None:
    """Run a find_matching probe's action for the current candidate (temp_scope carries `candidate`)."""
    if probe.action == "navigate":
        url = interpolate(probe.locator.url_template, temp_scope, field="probe.locator.url_template")
        await executor.page.goto(url)
    elif probe.action == "click":
        loc = await resolve_locator(executor.page, probe.locator, temp_scope, executor.evidence)
        await loc.click()
    else:
        raise ExecutorError(f"unsupported find_matching probe action: {probe.action!r}")


async def _find_matching(executor, action, scope) -> ActionResult:
    """Iterate candidates; capture the first whose probe checkpoint passes (schema-draft §5).

    `candidate` is visible ONLY inside the probe (a derived temp scope); it never leaks to the caller's
    scope. Exhaustion is a legitimate outcome (not a hard error) — we return it and let replay decide,
    plus an evidence screenshot per ADR-004.
    """
    try:
        candidate_list = scope.resolve(action.candidates)
    except KeyError:
        raise ExecutorError(f"find_matching candidates '{action.candidates}' is not in scope")
    if not isinstance(candidate_list, (list, tuple)):
        raise ExecutorError(f"find_matching candidates '{action.candidates}' is not a list "
                            f"(got {type(candidate_list).__name__})")

    for i, candidate in enumerate(candidate_list):
        temp = scope.derive(candidate=candidate)   # candidate lives only in this derived scope
        await _execute_probe(executor, action.probe, temp)
        # probe-check timeouts are expected iteration steps, not failures -> suppress their evidence shots
        cp = await executor.resolve_checkpoint(action.probe.checkpoint, temp, capture_evidence=False)
        _log.info("find_matching candidate %d/%d (%r) -> %s",
                  i + 1, len(candidate_list), candidate, cp.status)
        if cp.status == "success":
            scope.captures[action.capture.variable] = candidate   # bind into the CALLER's scope
            return ActionResult(status="success", action="find_matching",
                                bound_variable=action.capture.variable, bound_value=str(candidate),
                                candidates_tried=i + 1)

    shot = await executor.evidence.capture("find_matching_exhausted")
    return ActionResult(status="find_matching_exhausted", action="find_matching",
                        candidates_tried=len(candidate_list), screenshot_path=shot)
