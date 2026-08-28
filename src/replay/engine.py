"""ReplayEngine — deterministic orchestration of an Artifact over the executor (ADR-005/ADR-006/ADR-007).

Boundary: depends only on src/models + src/executor + the local replay modules. No LLM, no artifact YAML I/O
(the caller passes a parsed Artifact and a `artifact_loader` seam for fixtures), no real takeover UI (calls
the EscalationHandler seam), no failure-injection, no browser lifecycle (accepts a started executor).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Optional

from src.executor import ExecutorError, PlaywrightExecutor
from src.executor.interpolation import interpolate   # forward {{name}} substitution (executor is an allowed dep)
from src.models import Artifact
from src.safety import SafetyViolationError as SafetyBlockError   # replay-time safety BLOCK (terminal)

from . import generator
from .escalation_seam import EscalationContext, EscalationHandler, EscalationOutcome
from .results import CaptureBindingError, ReplayError, ReplayResult, SafetyViolationError
from .scope import VariableScope, new_scope

# Replay-layer conditions a HUMAN can resolve -> escalate (Phase-3 D1). Everything else the executor raises is
# a technical error a human cannot fix (Playwright crash, network, unhandled) -> hard_failure(technical_error).
_ESCALATABLE_ERRORS = (ExecutorError, SafetyViolationError)
_MAX_ESCALATION_ROUNDS = 3   # bound the escalate->retry->escalate loop; unresolved after this -> technical_error
# A source capture with a regex `extract` polls for its value (ParaBank populates detail fields via AJAX
# AFTER navigation, so a single immediate read races the render). Mirrors the checkpoint wait.
_CAPTURE_WAIT_MS = 5000
_CAPTURE_POLL_MS = 250

_log = logging.getLogger("replay")

_TEMPLATE_NAME = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def sensitive_bound_locators(artifact: Artifact) -> list:
    """models.Locators of `type_text` steps whose value interpolates a `sensitive: true` parameter — the input
    elements whose regions every evidence screenshot must mask (§3.4). Returns [] when nothing is declared
    sensitive, so the masking path is a no-op for non-credential capabilities."""
    params = getattr(artifact, "parameters", None)
    if params is None:
        return []
    sensitive = {name for name, p in params.properties.items() if getattr(p, "sensitive", None)}
    if not sensitive:
        return []
    out = []
    for step in artifact.steps:
        if getattr(step, "action", None) != "type_text":
            continue
        refs = {m.split(".")[0] for m in _TEMPLATE_NAME.findall(getattr(step, "value", "") or "")}
        if refs & sensitive and getattr(step, "locator", None) is not None:
            out.append(step.locator)
    return out


class ReplayEngine:
    def __init__(self, executor: PlaywrightExecutor, escalation_handler: EscalationHandler,
                 artifact_loader: Callable[[str], Artifact], safety_gate=None) -> None:
        self.executor = executor
        self.escalation_handler = escalation_handler
        self.artifact_loader = artifact_loader   # capability_name -> Artifact (seam for storage module)
        # Duck-typed safety gate (ADR-008 §Safety). None = off (default, unit tests); the CLI replay path
        # injects an enforcing SafetyGate. check_capability runs pre-flight below; the executor holds the same
        # gate for per-action checks. A block becomes hard_failure(reason="safety_blocked:<rule>") — no escalation.
        self.safety_gate = safety_gate

    # ---------------- entry point ----------------

    async def replay(self, artifact: Artifact, caller_parameters: dict[str, Any]) -> ReplayResult:
        # Safety pre-flight (ADR-008 §Safety): allowlist-domain coverage + capability_type sanity BEFORE the
        # step loop. A block is terminal (a human cannot un-break a mis-declared capability) -> hard_failure.
        if self.safety_gate is not None:
            try:
                self.safety_gate.check_capability(artifact)
            except SafetyBlockError as exc:
                return ReplayResult.hard_failure(reason=f"safety_blocked:{exc.rule_name}")
        # Validation data comes from caller_parameters (registry-backed, ADR-9); fixture composition removed.
        # Wire the escalation handler into the executor so `human_input` steps reach it (ADR-007 planned mode).
        self.executor.escalation_handler = self.escalation_handler
        params: dict[str, Any] = dict(caller_parameters)

        # generate-marker synthesis happens once, before steps, overriding any caller value.
        generated = generator.synthesize(artifact)
        if generated:
            _log.info("generated parameters: %s", generated)
        params.update(generated)

        scope = new_scope(params)

        # §3.4 evidence-screenshot masking: register the sensitive-bound input regions so any failure
        # screenshot obscures their values (declaration-driven; empty for non-credential capabilities).
        if hasattr(self.executor, "set_mask_locators"):
            self.executor.set_mask_locators(sensitive_bound_locators(artifact), scope)

        short_circuit = await self._run_steps(artifact, scope)
        if short_circuit is not None:
            return short_circuit

        outputs, missing = self._exported(artifact, scope)
        if missing:
            return ReplayResult.technical_error(
                f"exported names never populated: {missing} (ADR-006 Gap #4b)")
        return ReplayResult.success(outputs=outputs)

    # ---------------- step loop ----------------

    async def _run_steps(self, artifact: Artifact, scope: VariableScope) -> Optional[ReplayResult]:
        for step in artifact.steps:
            result, error = await self._exec(step, scope)

            # Safety BLOCK raised by the executor's pre-dispatch check_action (ADR-008 §Safety) — terminal, not
            # escalatable: convert to hard_failure(reason="safety_blocked:<rule>"). Checked before the generic
            # technical/escalation classification below.
            if isinstance(error, SafetyBlockError):
                return ReplayResult.hard_failure(reason=f"safety_blocked:{error.rule_name}", step_id=step.id)
            # Technical error (Playwright crash / network / unhandled) — a human cannot fix it (D1/D7): no escalation.
            if error is not None and not isinstance(error, _ESCALATABLE_ERRORS):
                return ReplayResult.technical_error(str(error), step_id=step.id)
            # A planned-intervention timeout means no human acted → terminal (D6 technical subtype).
            if error is None and result is not None and result.status == "human_input_timeout":
                return ReplayResult.technical_error("human_input_timeout", step_id=step.id)

            if error is not None or result.status != "success":
                # locator_exhausted / find_matching_exhausted / safety_violation → escalate (D1)
                decision, payload = await self._handle_action_failure(step, result, error, scope)
                if decision == "hard":
                    return payload                      # ReplayResult (stub_unavailable / human_aborted / technical)
                if decision == "continue":
                    continue                            # takeover: re-observe DOM, skip this step's binding
                result = payload                        # "ok": the successful retried ActionResult -> bind below

            try:
                await self._bind_captures(artifact, step, result, scope)
            except CaptureBindingError as exc:
                # A success capture can fail simply because the page reached a recognized BUSINESS outcome
                # (e.g. "Could not find account # ..."), which makes the capture moot (schema-draft §10). The
                # step's success phrases may reference this same failed capture, so success can't be evaluated
                # here — but expected_outcomes are capture-independent, so classify them FIRST (ADR-005
                # ordering rule). Only a genuine capture failure with NO matching business outcome is technical.
                biz = await self._business_outcome_fallback(artifact, step, scope)
                if biz is not None:
                    return biz
                # not in the D1 escalation set (not a stuck condition a takeover resolves) -> technical (D6)
                return ReplayResult.technical_error(str(exc), step_id=step.id)

            if step.checkpoint is not None:
                branch = await self._checkpoint_branch(artifact, step, scope)
                if branch is not None:
                    return branch   # business_outcome or hard_failure short-circuits remaining steps
        return None

    async def _exec(self, step, scope):
        """Execute one action, returning (ActionResult|None, error|None). An `_ESCALATABLE_ERRORS` instance is
        human-resolvable (locator/safety); any other exception is technical. A Step *is* the action."""
        try:
            return await self.executor.execute_action(step, scope), None
        except Exception as exc:  # noqa: BLE001 - classified by the caller (escalatable vs technical)
            return None, exc

    # ---------------- capture binding ----------------

    async def _bind_captures(self, artifact: Artifact, step, action_result, scope: VariableScope) -> None:
        for cap in [c for c in artifact.captures if c.source_step == step.id]:
            if cap.source is None:
                if step.action == "find_matching":
                    if cap.name not in scope.captures:   # executor binds this during execute_action
                        raise CaptureBindingError(
                            f"find_matching step '{step.id}' did not bind capture '{cap.name}'")
                elif step.action == "read_text":
                    scope.captures[cap.name] = action_result.text
                else:
                    raise CaptureBindingError(
                        f"capture '{cap.name}' has no source and step '{step.id}' "
                        f"(action={step.action}) produces no value")
            else:
                scope.captures[cap.name] = await self._extract_source(cap, scope)

    async def _extract_source(self, cap, scope: VariableScope) -> Any:
        src = cap.source
        if src.extract is not None and src.extract.all:
            return await self._extract_all(cap, scope)     # list capture (string[]), e.g. find_matching.candidates
        try:
            loc = await self.executor.resolve_locator(src.locator, scope)   # single element (N==1 enforced)
        except ExecutorError as exc:
            raise CaptureBindingError(f"capture '{cap.name}': could not resolve source locator: {exc}")
        if src.extract is None:
            return await self._read(loc, src.extract)          # no pattern -> nothing to wait for
        # Poll until the regex matches (async render) or the budget elapses; the final attempt raises the
        # informative CaptureBindingError. Fixes the capture-async-render race (values populated by AJAX after
        # navigate, while the immediate read saw only the empty "Balance:" label).
        deadline = time.monotonic() + _CAPTURE_WAIT_MS / 1000.0
        while True:
            value = self._apply_extract_or_text(cap, await self._read(loc, src.extract), require_match=False)
            if value is not None:
                return value
            if time.monotonic() >= deadline:
                return self._apply_extract_or_text(cap, await self._read(loc, src.extract))   # -> raises
            await asyncio.sleep(_CAPTURE_POLL_MS / 1000.0)

    async def _extract_all(self, cap, scope: VariableScope) -> list[str]:
        """List extraction (extract.all): match MANY elements and extract from each. resolve_locator can't
        be used (it demands N==1), so we build the selector for the css/href_pattern strategies list captures
        use (per schema-draft §10.1 account_ids) and iterate .all()."""
        loc = cap.source.locator
        strat = loc.strategy.value if loc.strategy is not None else None
        if strat in ("css", "css_id") or loc.css:
            selector = interpolate(loc.css, scope, "capture.source.locator.css")
        elif strat == "href_pattern" or loc.href_pattern:
            frag = interpolate(loc.href_pattern, scope, "capture.source.locator.href_pattern")
            selector = f"a[href*={json.dumps(frag)}]"
        else:
            raise ReplayError(f"capture '{cap.name}': list extraction (extract.all) supports only css / "
                              f"href_pattern locators (got strategy={loc.strategy})")
        elements = await self.executor.page.locator(selector).all()
        out: list[str] = []
        for el in elements:
            raw = await self._read(el, cap.source.extract)
            value = self._apply_extract_or_text(cap, raw, require_match=False)
            if value is not None:
                out.append(value)
        return out

    @staticmethod
    async def _read(pw_locator, extract) -> str:
        frm = extract.from_ if extract is not None else None
        if frm in (None, "text"):
            return await pw_locator.inner_text()
        return await pw_locator.get_attribute(frm) or ""

    @staticmethod
    def _apply_extract_or_text(cap, raw: str, require_match: bool = True):
        extract = cap.source.extract
        if extract is None:
            return raw
        m = re.search(extract.pattern, raw)
        if not m:
            if require_match:
                raise CaptureBindingError(
                    f"capture '{cap.name}': pattern {extract.pattern!r} not found in {raw!r}")
            return None
        return m.group(1) if m.groups() else m.group(0)

    async def _business_outcome_fallback(self, artifact, step, scope) -> Optional[ReplayResult]:
        """When a success capture on `step` fails, consult the step's checkpoint's expected_outcomes
        (capture-independent) before calling the failure technical (ADR-005 ordering). Returns a
        business_outcome ReplayResult if the page matches a declared outcome, else None."""
        cp_spec = getattr(step, "checkpoint", None)
        if cp_spec is None or not cp_spec.expected_outcomes:
            return None
        # capture_evidence=False: a no-match here isn't a real checkpoint timeout (the capture already failed
        # and will be reported) — don't write a throwaway screenshot.
        cp = await self.executor.resolve_checkpoint(cp_spec, scope, capture_evidence=False, business_only=True)
        if cp.status == "business_outcome":
            outputs, _ = self._exported(artifact, scope, strict=False)
            return ReplayResult.business_outcome(name=cp.outcome_name, observed_text=cp.observed_text,
                                                 partial_captures=outputs)
        return None

    # ---------------- checkpoint three-way branching (ADR-005 + ADR-007 resume table) ----------------

    async def _checkpoint_branch(self, artifact: Artifact, step, scope: VariableScope) -> Optional[ReplayResult]:
        cp = await self.executor.resolve_checkpoint(step.checkpoint, scope)
        if cp.status == "success":
            return None
        if cp.status == "business_outcome":
            outputs, _ = self._exported(artifact, scope, strict=False)
            return ReplayResult.business_outcome(name=cp.outcome_name, observed_text=cp.observed_text,
                                                 partial_captures=outputs)

        # checkpoint_timeout: a stuck condition -> escalate (D1). Stub / non-interactive -> no human reachable.
        expected = "; ".join(step.checkpoint.success.required_phrases)
        if not self.escalation_handler.is_interactive:
            return ReplayResult.stub_unavailable(step_id=step.id, expected=expected,
                                                 observed_text=cp.observed_text, screenshot_path=cp.screenshot_path)
        for _ in range(_MAX_ESCALATION_ROUNDS):
            outcome = await self._escalate(step, "checkpoint_timeout", cp.observed_text, cp.screenshot_path)
            if outcome.action == "abort":
                return ReplayResult.human_aborted(step_id=step.id, expected=expected, observed_text=cp.observed_text,
                                                  screenshot_path=cp.screenshot_path, operator_note=outcome.operator_note)
            if outcome.action == "exhausted":           # no human response / take-over timed out -> exhausted
                return ReplayResult.escalation_exhausted("no_response_or_takeover_timeout", step_id=step.id,
                                                         expected=expected, observed_text=cp.observed_text,
                                                         screenshot_path=cp.screenshot_path,
                                                         operator_note=outcome.operator_note)
            if outcome.action == "takeover_resume":     # human did their thing + clicked Done -> re-observe once
                cp = await self.executor.resolve_checkpoint(
                    step.checkpoint.model_copy(update={"wait_ms": step.checkpoint.poll_interval_ms}), scope)
            else:                                        # resume: re-poll with a fresh wait_ms budget
                cp = await self.executor.resolve_checkpoint(step.checkpoint, scope)
            if cp.status == "success":
                return None
            if cp.status == "business_outcome":
                outputs, _ = self._exported(artifact, scope, strict=False)
                return ReplayResult.business_outcome(name=cp.outcome_name, observed_text=cp.observed_text,
                                                     partial_captures=outputs)
            # still checkpoint_timeout -> escalate again (bounded loop)
        return ReplayResult.escalation_exhausted("checkpoint_unresolved_after_escalation", step_id=step.id,
                                                 expected=expected, observed_text=cp.observed_text,
                                                 screenshot_path=cp.screenshot_path)

    # ---------------- action-failure escalation (ADR-007 locator/find_matching/safety triggers) ----------------

    async def _handle_action_failure(self, step, result, error, scope):
        """Escalate a stuck ACTION and act on the human's choice, bounded. Returns (decision, payload):
          ('hard', ReplayResult) -> stub_unavailable / human_aborted / technical_error
          ('continue', None)     -> takeover_resume: re-observe DOM, skip this step's binding
          ('ok', ActionResult)   -> resume retried successfully; caller binds from this result (D1)."""
        reason = ("safety_violation" if isinstance(error, SafetyViolationError)
                  else "locator_exhausted" if error is not None else (result.status or "action_failed"))
        observed = getattr(error, "aria", "") or "" if error is not None else ""
        shot = getattr(error, "screenshot_path", None) if error is not None else result.screenshot_path
        if not self.escalation_handler.is_interactive:
            return "hard", ReplayResult.stub_unavailable(step_id=step.id, observed_text=observed, screenshot_path=shot)
        for _ in range(_MAX_ESCALATION_ROUNDS):
            outcome = await self._escalate(step, reason, observed, shot)
            if outcome.action == "abort":
                return "hard", ReplayResult.human_aborted(step_id=step.id, observed_text=observed,
                                                          screenshot_path=shot, operator_note=outcome.operator_note)
            if outcome.action == "exhausted":            # no human response / take-over timed out -> exhausted
                return "hard", ReplayResult.escalation_exhausted("no_response_or_takeover_timeout", step_id=step.id,
                                                                 observed_text=observed, screenshot_path=shot,
                                                                 operator_note=outcome.operator_note)
            if outcome.action == "takeover_resume":
                return "continue", None
            result, error = await self._exec(step, scope)     # resume: retry the action
            if error is not None and not isinstance(error, _ESCALATABLE_ERRORS):
                return "hard", ReplayResult.technical_error(str(error), step_id=step.id)
            if error is None and result.status == "success":
                return "ok", result
            observed = getattr(error, "aria", "") or "" if error is not None else ""
            shot = getattr(error, "screenshot_path", None) if error is not None else result.screenshot_path
        return "hard", ReplayResult.escalation_exhausted("action_unresolved_after_escalation", step_id=step.id,
                                                         observed_text=observed, screenshot_path=shot)

    async def _escalate(self, step, reason: str, observed_text: str,
                        screenshot_path: Optional[str]) -> EscalationOutcome:
        url = self.executor.page.url if self.executor.page else ""
        hint = step.metadata.escalation_hint if getattr(step, "metadata", None) else None
        return await self.escalation_handler.escalate(EscalationContext(
            step_id=step.id, reason=reason, current_url=url,
            observed_text=observed_text or "", screenshot_path=screenshot_path, hint=hint))

    # ---------------- output assembly ----------------

    def _exported(self, artifact: Artifact, scope: VariableScope, strict: bool = True):
        """Assemble exported captures ∪ exported parameters into the return contract.
        Returns (outputs, missing). `missing` (only meaningful when strict) lists exported names not bound."""
        outputs: dict[str, Any] = {}
        missing: list[str] = []
        for cap in artifact.captures:
            if cap.export:
                key = cap.returned_as or cap.name
                if cap.name in scope.captures:
                    outputs[key] = scope.captures[cap.name]
                elif strict:
                    missing.append(cap.name)
        if artifact.parameters is not None:
            for name, param in artifact.parameters.properties.items():
                if param.export:
                    key = param.returned_as or name
                    if name in scope.parameters:
                        outputs[key] = scope.parameters[name]
                    elif strict:
                        missing.append(name)
        return outputs, missing
