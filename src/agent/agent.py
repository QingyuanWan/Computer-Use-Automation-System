"""DiscoveryAgent — the LLM-driven discovery loop that produces an Artifact (ADR-004/005/006/008).

Boundary (ADR-008): imports only src/models + src/executor + src/replay + anthropic + the local agent
modules. This is the ONLY module permitted to call the LLM. It does not write YAML (storage), render
takeover UI (escalation), or enforce allowlists (safety). Part 2 adds the failure-injection sub-run and the
auto-replay validation gate (both owned by agent/ per ADR-008), which compose Part 1 discovery with
src/replay's ReplayEngine.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import anthropic

from src.executor import ExecutorError, LocatorResolutionError, PlaywrightExecutor, VariableScope
from src.models import Artifact, CapabilityType, ExpectedOutcome, HumanInputAction
from src.replay.escalation_seam import EscalationContext, EscalationHandler, StubEscalationHandler
from src.replay.results import SafetyViolationError
from src.safety import PIIRedactor
from src.safety import SafetyViolationError as SafetyBlockError   # runtime-gate BLOCK (terminal) — distinct
                                                                 # from the escalation-seam SafetyViolationError

from . import emission, hints, messages, tools
from .config import DEFAULT_MODEL, load_api_key
from .failure_injection import run_failure_injection
from .results import DiscoveryResult, FinishValidationError, RecordedStep, TranslationError
from .translation import to_action
from .validation_gate import run_validation_gate

_log = logging.getLogger("agent")

# Phase-3 discovery-time escalation triggers (ADR-007). Only fire when the handler can reach a human
# (is_interactive); with the stub, discovery keeps its Phase-1/2 termination (max_steps / stuck), so the
# existing stub-based loop tests are unchanged.
_DISCOVERY_LOCATOR_ESCALATION_THRESHOLD = 3   # D2: consecutive locator fails before escalating
_DISCOVERY_EXTRA_STEPS_ON_RESUME = 5          # contract 9: extra step budget granted on a max_steps resume


def _billed(usage: dict[str, int]) -> int:
    """Billed tokens for one sub-run: uncached input + output + cache writes (cache reads are ~free)."""
    return usage.get("input", 0) + usage.get("output", 0) + usage.get("cache_write", 0)


def _sum_usages(usages: list[dict[str, int]]) -> dict[str, int]:
    agg = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for u in usages:
        for k in agg:
            agg[k] += u.get(k, 0)
    return agg


def _flip_validated(artifact: Artifact) -> Artifact:
    """Return a copy of the artifact with metadata.validated = True (models are frozen)."""
    return artifact.model_copy(update={"metadata": artifact.metadata.model_copy(update={"validated": True})})


def _with_validation_skip_reason(artifact: Artifact, reason: str) -> Artifact:
    """Return a copy with metadata.validation_skip_reason set (validated stays False)."""
    return artifact.model_copy(
        update={"metadata": artifact.metadata.model_copy(update={"validation_skip_reason": reason})})


def _attach_expected_outcomes(artifact: Artifact, outcomes: list[ExpectedOutcome]):
    """Attach injection-authored expected_outcomes to the LAST step that carries a success checkpoint (the
    terminal business-outcome point). Returns (artifact, attached: bool)."""
    idx = None
    for i, s in enumerate(artifact.steps):
        if getattr(s, "checkpoint", None) is not None:
            idx = i
    if idx is None:
        return artifact, False
    step = artifact.steps[idx]
    new_cp = step.checkpoint.model_copy(update={"expected_outcomes": list(outcomes)})
    new_step = step.model_copy(update={"checkpoint": new_cp})
    new_steps = list(artifact.steps)
    new_steps[idx] = new_step
    return artifact.model_copy(update={"steps": new_steps}), True


class DiscoveryAgent:
    def __init__(self, executor: PlaywrightExecutor, *, model: str = DEFAULT_MODEL,
                 api_key: Optional[str] = None, client: Optional[Any] = None,
                 escalation_handler: Optional[EscalationHandler] = None,
                 evidence_root: Path = Path("evidence"), max_steps: int = 25,
                 max_repeated_failures: int = 3, safety_gate=None, state_fingerprint=None) -> None:
        # fail fast if no key (unless a client is injected for tests)
        self.client = client or anthropic.AsyncAnthropic(api_key=load_api_key(api_key))
        self.executor = executor
        # D3 fix (ADR-008 §Safety): an enforcing SafetyGate applied to discovery too — the same allowlist
        # check_action the replay executor runs, invoked in _handle_tool BEFORE dispatch. None = off (tests /
        # backward compat); the CLI injects an enforcing gate. An off-allowlist action is TERMINAL here
        # (status="safety_blocked"), not escalatable — mirroring the replay-time block.
        self._safety_gate = safety_gate
        # Slice 1d: an INJECTED, opaque state-fingerprint provider (a zero-arg async callable returning a
        # comparable snapshot). The agent never inspects the snapshot — it only compares before/after to enforce
        # a declared `read`. None => the state-delta check is skipped and the read is recorded as unverified
        # (metadata.state_verified stays null). The ParaBank implementation (account-overview table) is wired by
        # the launcher, so the agent stays surface-agnostic (REPORT §4: target/Playwright specifics never leak
        # into the layers above the Executor). Cost: one extra provider call (a login + overview read) per read
        # discovery, on each side of the run.
        self._state_fingerprint = state_fingerprint
        self.model = model
        # ADR-007: the agent invokes escalation_handler.escalate_planned when the LLM calls request_human_input
        # during discovery. Defaults to the stub (no-op planned mode) for backward compat; the CLI injects the
        # real overlay handler. The same handler is threaded into the validation gate + replay.
        self.escalation_handler = escalation_handler or StubEscalationHandler()
        self.evidence_root = Path(evidence_root)
        self.max_steps = max_steps
        self.max_repeated_failures = max_repeated_failures

    # ---------------- observation ----------------

    async def _observe(self) -> str:
        url = self.executor.page.url
        aria = await self.executor.page.locator("body").aria_snapshot()
        return f"URL: {url}\n\nARIA snapshot:\n{aria}"

    async def _screenshot_b64(self) -> str:
        return base64.b64encode(await self.executor.page.screenshot(full_page=False)).decode()

    # ---------------- main entry ----------------

    async def discover(self, goal: str, target_url: str, capability_name: str,
                       caller_parameters: "dict[str, str] | None" = None,
                       caller_parameter_sources: "dict[str, str] | None" = None,
                       *, capability_type: CapabilityType, generate_hints: bool = True) -> DiscoveryResult:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        evidence_dir = self.evidence_root / f"discovery_{capability_name}_{ts}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        # PII redaction of persisted evidence (ADR-008 §Safety): credential caller-param VALUES (and any
        # password/ssn/pin field) are replaced with [REDACTED] in the .txt/.json evidence before it is written.
        # Screenshots are unchanged (deferred, REPORT §7). No artifact exists yet during discovery, so the
        # field-name fallback + the caller_parameters values drive redaction.
        self._redactor = PIIRedactor()
        self._caller_params = dict(caller_parameters or {})

        system_blocks = tools.build_system_blocks(goal, target_url, caller_parameters)
        scope = VariableScope(parameters={}, captures={})
        await self.executor.page.goto(target_url)

        msgs: list[dict[str, Any]] = []
        pending_results: Optional[list[dict[str, str]]] = None
        observations: list[str] = []              # observations[turn-1] = ARIA captured at the top of `turn`
        recorded: list[RecordedStep] = []         # each carries its execution turn; windows assigned post-loop
        screenshot_next = False                   # ARIA-only default (ADR-004)
        consecutive: dict[str, int] = {}
        consecutive_locator_fails = 0             # D2: reset on any success; escalate at the threshold
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        finish_payload: Optional[dict[str, Any]] = None
        finish_turn: Optional[int] = None
        status = "max_steps"
        interactive = self.escalation_handler.is_interactive   # gate: no human -> keep Phase-1/2 termination
        budget = self.max_steps                   # may be extended once by a max_steps escalation resume
        max_steps_escalated = False
        turn = 0
        wall0 = time.time()

        while turn < budget:
            turn += 1
            aria = await self._observe()
            observations.append(aria)

            shot_b64 = await self._screenshot_b64() if screenshot_next else None
            screenshot_next = False
            self._write_observation(evidence_dir, turn, aria, shot_b64)

            msgs.append(messages.build_user_turn(pending_results, aria, shot_b64))
            messages.apply_cache_breakpoints(msgs)

            resp = await self.client.messages.create(
                model=self.model, max_tokens=2000, system=system_blocks, messages=msgs, tools=tools.TOOLS_CACHED)
            self._tally_usage(usage, resp.usage)
            self._write_response(evidence_dir, turn, resp)

            msgs.append({"role": "assistant", "content": [messages.block_to_dict(b) for b in resp.content]})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                status = "no_tool_call"
                break

            pending_results = []
            finished = False
            safety_hit = False
            safety_blocked = False
            actions_rec: list[dict[str, Any]] = []
            results_rec: list[dict[str, Any]] = []
            for idx, block in enumerate(tool_uses):
                step_id = f"step_{turn:02d}" + ("" if len(tool_uses) == 1 else f"_{idx}")
                r = await self._handle_tool(block, step_id, turn, scope, consecutive, recorded, pending_results)
                actions_rec.append({"name": block.name, "input": block.input})
                results_rec.append(r)
                st = r.get("status")
                if st == "locator_failed":
                    consecutive_locator_fails += 1
                elif st == "success":
                    # a successful action is progress (D2): it clears BOTH the locator-fail run and the
                    # per-tool repeated-failure counters, so an intervening success resets "stuck"/escalation.
                    consecutive_locator_fails = 0
                    consecutive.clear()
                elif st == "safety_violation":
                    safety_hit = True
                elif st == "safety_blocked":            # D3: runtime-gate block -> terminal, not escalatable
                    safety_blocked = True
                if block.name == "finish" and st == "finish":
                    finish_payload = block.input
                    finished = True
                if r.get("screenshot_next"):
                    screenshot_next = True
            self._write_actions(evidence_dir, turn, actions_rec, results_rec)

            if finished:
                finish_turn = turn
                status = "success"
                break

            # D3: a runtime safety-gate block is TERMINAL (the LLM tried to leave the allowed surface / use an
            # unsanctioned action) — stop discovery cleanly, no escalation, no artifact.
            if safety_blocked:
                status = "safety_blocked"
                break

            # Contracts 8 + 10: a stuck locator run (D2) or a safety violation is escalatable — hand to the human
            # if one is reachable. Resume/take-over -> record a HumanInputAction (D3) and continue; Abort -> stop.
            if interactive and (safety_hit
                                or consecutive_locator_fails >= _DISCOVERY_LOCATOR_ESCALATION_THRESHOLD):
                reason = "safety_violation" if safety_hit else "locator_exhausted"
                action = await self._discovery_escalate(reason, f"step_{turn:02d}_escalation", turn, recorded)
                if action == "abort":
                    status = "aborted"
                    break
                consecutive_locator_fails = 0      # the human intervened; clear the stuck counters
                consecutive.clear()
                continue

            # Non-escalatable repeated failures (or no human reachable) -> stuck (Phase-1/2 behavior).
            if any(c >= self.max_repeated_failures for c in consecutive.values()):
                status = "stuck"
                break

            # Contract 9: budget exhausted without a finish -> escalate once; Resume grants extra steps.
            if turn >= budget and interactive and not max_steps_escalated:
                action = await self._discovery_escalate("max_steps", f"step_{turn:02d}_escalation", turn, recorded)
                if action == "abort":
                    status = "aborted"
                    break
                budget += _DISCOVERY_EXTRA_STEPS_ON_RESUME
                max_steps_escalated = True

        if status == "success" and finish_turn is not None:
            _assign_observation_windows(recorded, observations, finish_turn)

        wall = time.time() - wall0
        target_hint = urllib.parse.urlparse(target_url).hostname
        result = self._finalize(status, finish_payload, recorded, capability_name, evidence_dir, usage, wall,
                                capability_type, target_hint, caller_parameters, caller_parameter_sources)
        # ADR-007 revision (D1-α): author operator hints via a SECONDARY LLM call and attach them to the
        # emitted artifact. Skipped for injection sub-runs (generate_hints=False) so throwaway artifacts don't
        # pay for hints. `usage` is the same dict _finalize stored on the result, so tallying here rolls the
        # hint cost into result.usage / total_billed_tokens.
        if generate_hints and result.status == "success" and result.artifact is not None:
            result.artifact = await self._attach_escalation_hints(
                result.artifact, goal, target_url, caller_parameters or {}, usage)
        return result

    async def _attach_escalation_hints(self, artifact, goal, target_url, caller_parameters, usage):
        """Generate + attach escalation hints (ADR-007 revision). Best-effort: any failure leaves the artifact
        hint-less rather than failing discovery. Hints are reverse-parameterized against caller_parameters so
        no session-specific value is persisted (contract item 3)."""
        raw, hint_usage = await hints.generate_step_hints(self.client, self.model, artifact, goal, target_url)
        if hint_usage is not None:
            self._tally_usage(usage, hint_usage)
        return hints.attach_hints(artifact, raw, dict(caller_parameters))

    # ---------------- Part 2: discovery + injection + validation orchestration ----------------

    async def discover_and_validate(self, goal: str, target_url: str, capability_name: str, *,
                                    capability_type: CapabilityType,
                                    caller_parameters: "dict[str, str] | None" = None,
                                    caller_parameter_sources: "dict[str, str] | None" = None,
                                    validation_headless: bool = True) -> DiscoveryResult:
        """Full cycle: happy-path discovery -> (read only) failure-injection -> auto-replay validation gate.
        Returns a DiscoveryResult whose artifact has validated flipped, plus cumulative cost + injection +
        validation metadata. Validation data comes from caller_parameters (recorded in
        metadata.sample_invocation and reused by the gate, ADR-9). The Artifact (and its metadata) are frozen,
        so each stage rebinds a fresh copy rather than mutating in place; the mutable DiscoveryResult carries
        the accumulating run metadata."""
        # Slice 1d: snapshot the target's opaque state BEFORE the discovery body (declared read + provider only),
        # so a mis-declared mutation can be detected after. One extra provider call (login + overview read).
        fingerprint_before = None
        if capability_type == CapabilityType.read and self._state_fingerprint is not None:
            fingerprint_before = await self._state_fingerprint()
        result = await self.discover(goal, target_url, capability_name, caller_parameters,
                                     caller_parameter_sources, capability_type=capability_type)
        usages = [result.usage]
        result.total_billed_tokens = _billed(result.usage)
        if result.status != "success" or result.artifact is None:
            result.detail = (result.detail or "") + " | injection+validation skipped: happy-path did not succeed"
            return result

        artifact = result.artifact

        # --- Slice 1d: state-delta enforcement of a DECLARED read, BEFORE injection (injection re-drives the
        # flow and would itself mutate a mis-declared one). Compare the injected opaque fingerprint before/after
        # the body: read + delta => refuse emission; no provider => skip and record via metadata.state_verified
        # so an unverified declaration never looks verified. Residual (REPORT §Determinism): detects what the
        # RUN did, not what the capability COULD do. ---
        if artifact.metadata.capability_type == CapabilityType.read:
            if self._state_fingerprint is None:
                result.warnings.append("read capability NOT state-verified: no state_fingerprint provider "
                                       "injected (metadata.state_verified stays null)")
            else:
                fingerprint_after = await self._state_fingerprint()
                try:
                    emission.assert_read_did_not_mutate(artifact.metadata.capability_type,
                                                        fingerprint_before, fingerprint_after)
                except emission.ReadCapabilityMutatedError as exc:
                    result.status = "error"
                    result.detail = str(exc)
                    result.artifact = None
                    return result
                artifact = artifact.model_copy(
                    update={"metadata": artifact.metadata.model_copy(update={"state_verified": True})})
                result.artifact = artifact

        # --- failure-injection sub-run: READ capabilities only (destructive-injection guard, ADR-005) ---
        if artifact.metadata.capability_type == CapabilityType.read:
            result.injection_ran = True
            # Injection reuses caller_parameters (minus the swapped value): the OTHER params stay parameterized;
            # the injected invalid value is not a caller parameter, so it is never reverse-parameterized (ADR-9).
            outcomes, inj_usages, inj_warnings = await run_failure_injection(
                self, artifact, goal, target_url, capability_name, caller_parameters=caller_parameters)
            usages.extend(inj_usages)
            result.warnings.extend(inj_warnings)
            if outcomes:
                artifact, attached = _attach_expected_outcomes(artifact, outcomes)
                if attached:
                    result.expected_outcomes_added = len(outcomes)
                else:
                    result.warnings.append("failure-injection produced outcomes but the artifact has no "
                                           "checkpoint to attach them to; outcomes discarded")
            else:
                result.warnings.append("failure-injection ran but every strategy was discarded by the "
                                       "3-condition classifier; expected_outcomes left empty")
        else:
            result.injection_skip_reason = (
                f"capability_type={artifact.metadata.capability_type.value}: failure-injection skipped to "
                f"avoid destructive writes (ADR-005 destructive-injection guard)")
            _log.info(result.injection_skip_reason)

        # --- validation-gate skip: a capability needing a human cannot be auto-validated (Concern fix #1) ---
        # (human_input is checked first: a human_input step force-types the capability `mutating`, so the more
        # specific requires_human_input reason must win over the generic mutating skip below.)
        if any(s.action == "human_input" for s in artifact.steps):
            reason = "requires_human_input"
            artifact = _with_validation_skip_reason(artifact, reason)
            result.validation_status = "skipped_requires_human"
            result.validation_detail = ("capability contains a planned human-input step; auto-replay "
                                        "validation skipped (ADR-007). validated stays false.")
            _log.info("validation gate skipped: %s", result.validation_detail)
        elif artifact.metadata.capability_type == CapabilityType.mutating:
            # A mutating capability CANNOT be auto-validated by replaying it — the validation replay IS the
            # mutation (it performs the real, irreversible side effect again). So discovery would submit the
            # loan/transfer twice: once in the happy-path exploration (unavoidable — you cannot discover the flow
            # without performing it) and once more in the gate. Skip the gate; the capability ships
            # validated=false with this principle as the reason (REPORT §Determinism / §Cuts non-idempotency).
            reason = "mutating_not_auto_validatable"
            artifact = _with_validation_skip_reason(artifact, reason)
            result.validation_status = "skipped_mutating"
            result.validation_detail = ("capability_type=mutating: auto-replay validation skipped — a mutating "
                                        "capability cannot be validated by replaying it, because the replay IS "
                                        "the mutation (it would perform the real side effect again). validated "
                                        "stays false.")
            _log.info("validation gate skipped: %s", result.validation_detail)
        else:
            # --- auto-replay validation gate: fresh session, caller_parameters from sample_invocation (ADR-9) ---
            # The gate is UNATTENDED and runs in its OWN executor, so it must NOT borrow the discovery handler
            # (whose page_provider points at the discovery browser, and which would pop a panel on the wrong,
            # headed session). Let the gate use its non-interactive StubEscalationHandler default: any
            # unexpected stuck condition then fails fast as stub_unavailable instead of waiting on a panel.
            val = await run_validation_gate(artifact, target_url=target_url,
                                            evidence_dir=Path(result.evidence_dir), headless=validation_headless)
            result.validation_status = val.status
            result.validation_detail = val.reason or val.outcome_name
            if val.status == "success":
                artifact = _flip_validated(artifact)     # gate passed -> validated: true
            else:
                _log.info("validation gate did NOT pass (status=%s); keeping validated=false", val.status)

        result.artifact = artifact
        result.usage = _sum_usages(usages)
        result.total_billed_tokens = sum(_billed(u) for u in usages)
        return result

    # ---------------- per-tool handling ----------------

    async def _handle_tool(self, block, step_id, turn, scope, consecutive, recorded, pending_results) -> dict:
        name = block.name
        if name == "finish":
            try:
                _validate_finish(block.input)
            except FinishValidationError as exc:
                pending_results.append({"tool_use_id": block.id, "text": f"finish rejected: {exc}"})
                return {"status": "finish_invalid", "detail": str(exc)}
            pending_results.append({"tool_use_id": block.id, "text": "finish acknowledged."})
            return {"status": "finish"}

        if name == "request_screenshot":
            pending_results.append({"tool_use_id": block.id,
                                    "text": "A screenshot will be included with the next observation."})
            return {"status": "ok", "screenshot_next": True}

        if name == "request_human_input":
            # ADR-007 planned mode: pause discovery, let the present human act, then record a HumanInputAction
            # step so every replay pauses here too. Void — no value returns to the LLM.
            prompt = str(block.input.get("prompt", ""))
            reason = str(block.input.get("reason", ""))
            try:
                await self.escalation_handler.escalate_planned(prompt, reason)
                note = "Human input completed. Observe the NEW page state and continue."
            except asyncio.TimeoutError:
                note = "Human input timed out; observe the page and decide how to proceed."
            recorded.append(RecordedStep(
                step_id=step_id, action=HumanInputAction(id=step_id, prompt=prompt, reason=reason), turn=turn))
            pending_results.append({"tool_use_id": block.id, "text": note})
            return {"status": "human_input"}

        try:
            action = to_action(name, block.input, step_id)
        except TranslationError as exc:
            consecutive[name] = consecutive.get(name, 0) + 1
            pending_results.append({"tool_use_id": block.id, "text": f"`{name}` could not be built: {exc}"})
            return {"status": "translation_error", "detail": str(exc)}

        # D3: discovery-time safety gate — the SAME check_action the replay executor runs, applied before any
        # Playwright dispatch. An off-allowlist domain / unsanctioned action type is a TERMINAL block during
        # discovery (not escalatable), so the LLM cannot steer exploration off the target surface.
        if self._safety_gate is not None:
            try:
                self._safety_gate.check_action(action, scope)
            except SafetyBlockError as exc:
                pending_results.append({"tool_use_id": block.id, "text": f"`{name}` BLOCKED (safety): {exc}"})
                return {"status": "safety_blocked", "detail": str(exc)}

        try:
            result = await self.executor.execute_action(action, scope)
        except LocatorResolutionError as exc:     # ADR-004 trigger (a): 0-match -> screenshot next turn
            consecutive[name] = consecutive.get(name, 0) + 1
            pending_results.append({"tool_use_id": block.id, "text": f"`{name}` FAILED (no match): {exc}"})
            return {"status": "locator_failed", "detail": str(exc), "screenshot_next": True}
        except SafetyViolationError as exc:       # Phase-3 contract 10: a guardrail blocked the action -> escalatable
            consecutive[name] = consecutive.get(name, 0) + 1
            pending_results.append({"tool_use_id": block.id, "text": f"`{name}` BLOCKED (safety): {exc}"})
            return {"status": "safety_violation", "detail": str(exc)}
        except ExecutorError as exc:
            consecutive[name] = consecutive.get(name, 0) + 1
            pending_results.append({"tool_use_id": block.id, "text": f"`{name}` FAILED: {exc}"})
            return {"status": "executor_error", "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - raw backend errors (e.g. Playwright fill on a non-input
            # when the LLM targets the wrong element) are RECOVERABLE: record + let the LLM adapt next turn.
            consecutive[name] = consecutive.get(name, 0) + 1
            pending_results.append({"tool_use_id": block.id, "text": f"`{name}` FAILED: {exc}"})
            return {"status": "action_error", "detail": str(exc)}

        if result.status == "success":
            consecutive[name] = 0
        else:
            consecutive[name] = consecutive.get(name, 0) + 1

        recorded.append(RecordedStep(
            step_id=step_id, action=action, turn=turn, action_status=result.status,
            read_text_value=(result.text if name == "read_text" else None)))
        note = f"`{name}` -> {result.status} (url={result.resulting_url})"
        if name == "read_text":
            note += f" text={result.text!r}"
        pending_results.append({"tool_use_id": block.id, "text": note})
        return {"status": result.status}

    # ---------------- discovery-time escalation (Phase-3 contracts 8-11) ----------------

    async def _discovery_escalate(self, reason: str, step_id: str, turn: int, recorded: list) -> str:
        """Reactive escalation DURING discovery. Shows the panel; on resume/take-over records a HumanInputAction
        so replay deterministically re-triggers the same human intervention point (D3 reactive->planned
        transformation, contract 11). Returns the operator's action ('resume' | 'takeover_resume' | 'abort').
        Only called when the handler is interactive (a human is reachable)."""
        observed = await self._observe()
        outcome = await self.escalation_handler.escalate(EscalationContext(
            step_id=step_id, reason=reason, current_url=self.executor.page.url, observed_text=observed))
        if outcome.action != "abort":
            prompt = (f"A human operator resolved a '{reason}' condition here during discovery. "
                      f"Reproduce that manual step, then let replay continue.")
            recorded.append(RecordedStep(
                step_id=step_id, action=HumanInputAction(id=step_id, prompt=prompt, reason=reason), turn=turn))
        return outcome.action

    # ---------------- finalize / emit ----------------

    def _finalize(self, status, finish_payload, recorded, capability_name, evidence_dir, usage, wall,
                  capability_type=None, target_hint=None, caller_parameters=None, caller_parameter_sources=None):
        base = DiscoveryResult(status=status, capability_name=capability_name,
                               evidence_dir=str(evidence_dir), steps=len(recorded), usage=usage,
                               wall_s=round(wall, 1))
        if status != "success" or finish_payload is None:
            base.detail = f"discovery ended without a finish ({status})"
            return base
        if not recorded:
            base.status = "error"
            base.detail = "finish called but no steps were recorded"
            return base
        try:
            artifact, dropped, warnings = emission.emit_artifact(
                capability_name, recorded, dict(finish_payload["result"]),
                list(finish_payload["success_observed_phrases"]), model=self.model,
                capability_type=capability_type,
                target_app_hint=target_hint, caller_parameters=caller_parameters,
                caller_parameter_sources=caller_parameter_sources)
        except Exception as exc:  # noqa: BLE001 - surface emission failures as a typed result
            base.status = "error"
            base.detail = f"artifact emission failed: {exc}"
            return base
        base.artifact = artifact
        base.dropped_exports = dropped
        base.warnings = warnings
        return base

    # ---------------- evidence + usage ----------------

    @staticmethod
    def _tally_usage(agg, u):
        agg["input"] += getattr(u, "input_tokens", 0) or 0
        agg["output"] += getattr(u, "output_tokens", 0) or 0
        agg["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        agg["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

    def _redact(self, text: str) -> str:
        """Redact credential caller-param values (+ password/ssn/pin fields) from evidence text (ADR-008
        §Safety). No-op if the redactor was not initialised (defensive for partial test construction)."""
        redactor = getattr(self, "_redactor", None)
        if redactor is None:
            return text
        return redactor.redact(text, values=getattr(self, "_caller_params", None))

    def _write_observation(self, d: Path, turn: int, aria: str, shot_b64: Optional[str]) -> None:
        (d / f"step_{turn:02d}_observation_aria.txt").write_text(self._redact(aria), encoding="utf-8")
        if shot_b64:
            (d / f"step_{turn:02d}_observation_screenshot.png").write_bytes(base64.b64decode(shot_b64))

    def _write_response(self, d: Path, turn: int, resp) -> None:
        try:
            payload = resp.model_dump()
        except Exception:  # pragma: no cover - defensive for mocks
            payload = {"content": [getattr(b, "type", "?") for b in resp.content]}
        (d / f"step_{turn:02d}_llm_response.json").write_text(
            self._redact(json.dumps(payload, indent=2, default=str)), encoding="utf-8")

    def _write_actions(self, d: Path, turn: int, actions, results) -> None:
        (d / f"step_{turn:02d}_action.json").write_text(
            self._redact(json.dumps(actions, indent=2, default=str)), encoding="utf-8")
        (d / f"step_{turn:02d}_action_result.json").write_text(
            self._redact(json.dumps(results, indent=2, default=str)), encoding="utf-8")


def _assign_observation_windows(recorded, observations, finish_turn) -> None:
    """Set each recorded step's observation_after to its observation WINDOW: every observation captured
    after this step executes and up to (and including) the next recorded step's turn — for the last step,
    up to and including the finish-turn observation. This makes async-late-rendered values and the
    finish/post-request_screenshot observations traceable to a step (Phase-1 fix)."""
    for i, step in enumerate(recorded):
        end = recorded[i + 1].turn if i + 1 < len(recorded) else finish_turn
        window = [observations[t - 1] for t in range(step.turn + 1, end + 1) if 0 < t <= len(observations)]
        step.observation_after = "\n".join(window)


def _validate_finish(payload: dict[str, Any]) -> None:
    phrases = payload.get("success_observed_phrases")
    if not phrases or not isinstance(phrases, list):
        raise FinishValidationError("finish requires a non-empty success_observed_phrases list (ADR-005)")
    if "result" not in payload or not isinstance(payload["result"], dict):
        raise FinishValidationError("finish requires a result object")
