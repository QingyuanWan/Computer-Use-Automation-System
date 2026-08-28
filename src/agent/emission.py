"""Emission pipeline: recorded discovery steps + finish payload -> a Pydantic Artifact (validated=false).

Pure functions (no LLM, no Playwright) so the correctness-dense logic is fully unit-testable:
  - infer_capability_type  (ADR-005 Gap #0: pattern-based auth-prologue strip, NOT URL-based)
  - reverse_parameterize   (ADR-005 Gap #2: longest-first, token-boundary, skip <3 chars)
  - trace_to_step          (earliest observation step containing a value)
  - build_captures         (ADR-006 Gap #4b: untraceable finish fields are dropped, never export=true)
  - build_checkpoints      (ADR-005 Gap #1: phrases attach to their source step; multi-page -> per-step)
  - emit_artifact          (compose)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from src.models import (
    Artifact,
    ArtifactMetadata,
    CapabilityType,
    Capture,
    CaptureSource,
    Checkpoint,
    ExtractSpec,
    GenerateMarker,
    Locator,
    Parameter,
    ParametersBlock,
    SuccessCriteria,
)

from .credentials import CredentialsHardcodedError, detect_credential, looks_like_credential_value
from .results import RecordedStep

_log = logging.getLogger("agent.emission")


def _locator_names(action) -> tuple:
    """The human-facing name fields of an action's locator (for credential field-name detection)."""
    loc = getattr(action, "locator", None)
    if loc is None:
        return ()
    return (getattr(loc, "name", None), getattr(loc, "label", None),
            getattr(loc, "placeholder", None), getattr(loc, "id", None), getattr(loc, "text", None))

_LOGIN_RE = re.compile(r"log\s*in|sign\s*in|log\s*on", re.IGNORECASE)


# ---------------- capability_type: caller-declared, with a hard human_input override (Slice 1) ----------------
# The read/mutating label is DECLARED by the caller at discovery time (a required --capability-type flag), NOT
# inferred from the actions: no syntactic rule can tell a search box from a transfer box (the removed
# infer_capability_type "any post-login type_text => mutating" misclassified form-based reads). The label is a
# deliberate human safety assertion, enforced downstream (validator's human_input rule + the discovery
# state-delta probe). The ONE inference that remains is a conservative OVERRIDE: a capability that pauses for a
# human can change state unpredictably, so a human_input step forces `mutating` regardless of the declaration.

def apply_human_input_override(declared: CapabilityType, actions: list[Any]) -> CapabilityType:
    """Return `declared`, except force `mutating` when any action is a human_input step — a hard rule that
    beats the declaration (ADR-007 planned mode: a human may change state during the pause)."""
    if any(getattr(a, "action", None) == "human_input" for a in actions):
        return CapabilityType.mutating
    return declared


class ReadCapabilityMutatedError(Exception):
    """A capability DECLARED `read` changed observable target state during discovery (Slice 1d state-delta)."""


def assert_read_did_not_mutate(declared: CapabilityType, before: Any, after: Any) -> None:
    """Refuse a declared `read` whose discovery run changed the target's opaque state fingerprint.
    Residual (REPORT §Determinism): this detects what the RUN did, not what the capability COULD do — a
    sometimes-mutating flow that happened not to mutate on this run (e.g. a request that was denied) would
    pass. `before`/`after` are opaque comparable snapshots from an injected provider; the check never inspects
    their contents."""
    if declared == CapabilityType.read and before != after:
        raise ReadCapabilityMutatedError(
            "declared 'read' but the discovery run changed observable target state (state-delta refusal, "
            "Slice 1d) — re-declare the capability '--capability-type mutating'")


# ---------------- reverse-parameterization (ADR-005 Gap #2) ----------------

def reverse_parameterize(phrase: str, name_values: dict[str, str]) -> str:
    """Replace known parameter/capture VALUES in `phrase` with `{{name}}`. Guardrails: longest value first
    (so 25.00 wins over 25), token-boundary match (25 does not match inside 254), skip values <3 chars."""
    out = phrase
    for name, value in sorted(name_values.items(), key=lambda kv: len(str(kv[1])), reverse=True):
        v = str(value)
        if len(v) < 3:
            continue
        pattern = r"(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])"
        out = re.sub(pattern, "{{" + name + "}}", out)
    return out


# ---------------- step-field reverse-parameterization (ADR-9) ----------------
# Locator string fields that may hold a caller-supplied literal (role/strategy/index are NOT parameterized).
_LOCATOR_RP_FIELDS = ("name", "href_pattern", "css", "id", "text", "label", "placeholder", "url_template")


def _rp(text: Optional[str], param_values: dict[str, str]) -> tuple[Optional[str], set]:
    """Reverse-parameterize one string; return (new_text, names_substituted)."""
    if not text:
        return text, set()
    out = reverse_parameterize(text, param_values)
    used = {n for n in param_values if ("{{" + n + "}}") in out}
    return out, used


def _rp_locator(loc: Locator, param_values: dict[str, str]) -> tuple[Locator, set]:
    used: set = set()
    updates: dict[str, Any] = {}
    for f in _LOCATOR_RP_FIELDS:
        new, u = _rp(getattr(loc, f), param_values)
        used |= u
        if new != getattr(loc, f):
            updates[f] = new
    if loc.fallbacks:
        new_fbs = []
        changed = False
        for fb in loc.fallbacks:
            nfb, u = _rp_locator(fb, param_values)
            used |= u
            changed = changed or (nfb is not fb)
            new_fbs.append(nfb)
        if changed:
            updates["fallbacks"] = new_fbs
    return (loc.model_copy(update=updates) if updates else loc), used


def reverse_parameterize_action(action: Any, param_values: dict[str, str]) -> tuple[Any, set]:
    """Template caller-supplied literals out of a step's fields (TypeText.value, Navigate.url, and every
    Locator string field, recursing find_matching.probe.locator + fallbacks). Returns (new_action,
    names_substituted). Uses caller_parameters ∪ generated values — NOT captures (those flow via
    find_matching/{{capture}} already)."""
    if not param_values:
        return action, set()
    used: set = set()
    updates: dict[str, Any] = {}
    loc = getattr(action, "locator", None)
    if loc is not None:
        nloc, u = _rp_locator(loc, param_values)
        used |= u
        if nloc is not loc:
            updates["locator"] = nloc
    if action.action == "type_text":
        nval, u = _rp(action.value, param_values)
        used |= u
        if nval != action.value:
            updates["value"] = nval
    elif action.action == "navigate":
        nurl, u = _rp(action.url, param_values)
        used |= u
        if nurl != action.url:
            updates["url"] = nurl
    elif action.action == "find_matching":
        nploc, u = _rp_locator(action.probe.locator, param_values)
        used |= u
        if nploc is not action.probe.locator:
            updates["probe"] = action.probe.model_copy(update={"locator": nploc})
    return (action.model_copy(update=updates) if updates else action), used


def _infer_param_type(value: str) -> str:
    """Conservative JSON-Schema type from a string value: decimals -> number, true/false -> boolean, else
    string. Integer-looking ids (e.g. account 12345) stay `string` on purpose (schema-draft §3 Example A)."""
    v = str(value).strip()
    if v.lower() in ("true", "false"):
        return "boolean"
    if re.fullmatch(r"-?\d+\.\d+", v):
        return "number"
    return "string"


def _build_parameters(caller_parameters: dict[str, str], generated_parameters: dict[str, str],
                      auto_credentials: "set[str] | None" = None) -> Optional[ParametersBlock]:
    """Declare the parameters block. A caller parameter whose NAME matches a credential pattern is marked
    `sensitive: true`; `auto_credentials` are the credential params B2 auto-declared from named credential
    steps (also `sensitive: true`, string-typed, and caller-required at replay). Generated params are
    synthesized, not caller-required (ADR-010)."""
    auto_credentials = auto_credentials or set()
    props: dict[str, Parameter] = {}
    for name, value in caller_parameters.items():
        sensitive = True if detect_credential(name) else None
        props[name] = Parameter(type=_infer_param_type(value), sensitive=sensitive)
    for name in generated_parameters:
        props[name] = Parameter(type="string", generate=GenerateMarker.unique_string)
    for name in sorted(auto_credentials):
        if name not in props:
            props[name] = Parameter(type="string", sensitive=True)
    if not props:
        return None
    # caller-supplied + auto-declared credential params are required; generated ones are synthesized.
    required = sorted(set(caller_parameters) | set(auto_credentials))
    return ParametersBlock(properties=props, required=required)


# ---------------- tracing ----------------

def trace_to_step(value: str, steps: list[RecordedStep]) -> Optional[str]:
    """step_id of the earliest recorded step whose observation_after contains `value` (else None)."""
    v = str(value)
    for step in steps:
        if v and v in (step.observation_after or ""):
            return step.step_id
    return None


# ---------------- capture-pattern generalization (cross-tenant replay) ----------------
# A source-extract capture reads its value from the page at REPLAY time (balance, dates, ids). Hardcoding the
# discovery-session literal into the extract pattern breaks cross-tenant replay: tenant B's balance ($425.50)
# never matches tenant A's literal (\$415\.50) → CaptureBindingError → hard_failure. At emission, a value that
# matches a known dynamic-data SHAPE is replaced with the generic regex for that shape; anything else keeps its
# exact escaped literal (existing behavior — a stable label like CHECKING, or a specific error code that SHOULD
# match exactly, is preserved). Library is deliberately closed: only currency/date/number (user-scoped).
# Trade-off (by design): a capture whose VALUE is a pure integer (account number, id) is generalized to \d+ —
# the desired cross-tenant behavior, but it means a numeric value intended for exact match cannot be a capture.
# Exact-match error codes/enums live in checkpoint required_phrases (build_checkpoints -> reverse_parameterize),
# a SEPARATE path that never touches this library, so they stay literal.
CAPTURE_PATTERN_LIBRARY: list[tuple[str, str, str]] = [
    # (matcher_regex, replacement_regex, description) — first match wins
    (r"^\$[\d,]+\.\d{2}$", r"\$[\d,]+\.\d{2}", "currency"),
    (r"^\d{2}[-/]\d{2}[-/]\d{4}$", r"\d{2}[-/]\d{2}[-/]\d{4}", "date"),
    (r"^\d+$", r"\d+", "number"),
]


def _match_capture_pattern(value: str) -> "tuple[str, str] | None":
    """Return (replacement_regex, description) if `value` matches a known dynamic-data shape, else None.
    First match wins. Kept separate so build_captures can log the matched description."""
    for matcher, replacement, desc in CAPTURE_PATTERN_LIBRARY:
        if re.match(matcher, value):
            return replacement, desc
    return None


def _generalize_capture_pattern(value: str) -> str:
    """Map a session-specific observed value to a generic regex if it matches a known dynamic-data shape
    (currency/date/number); otherwise return the exact escaped literal (existing behavior). First match wins."""
    match = _match_capture_pattern(value)
    return match[0] if match else re.escape(value)


# ---------------- captures from finish(result) (ADR-006 Gap #4b) ----------------

def build_captures(finish_result: dict[str, Any], steps: list[RecordedStep],
                   caller_parameters: Optional[dict[str, str]] = None):
    """Return (captures, dropped_names, warnings). A finish field traced to a source step becomes an
    export=true Capture; an untraceable one (e.g. an LLM-computed value never on the page) is DROPPED with
    a warning and never exported (ADR-006 Gap #4b). A finish field that merely echoes a caller_parameter
    (same name, or same value) is ALSO dropped: it is an input echo, not a discovered output, and a
    same-named capture would collide with the parameter at model validation (ADR-9)."""
    caller_parameters = caller_parameters or {}
    caller_values = {str(v) for v in caller_parameters.values()}
    captures: list[Capture] = []
    dropped: list[str] = []
    warnings: list[str] = []
    by_id = {s.step_id: s for s in steps}
    for name, value in finish_result.items():
        sval = str(value)
        if name in caller_parameters or sval in caller_values:
            dropped.append(name)
            warnings.append(f"finish field '{name}'={sval!r} echoes a caller parameter; dropped from "
                            f"exports (input echo, not a discovered output — ADR-9)")
            continue
        step_id = trace_to_step(sval, steps)
        if step_id is None:
            dropped.append(name)
            warnings.append(f"finish field '{name}'={sval!r} could not be traced to any observation; "
                            f"dropped from exports (ADR-006 Gap #4b)")
            continue
        step = by_id[step_id]
        if step.action.action == "read_text" and step.read_text_value == sval:
            source = None                                 # bound from the read_text action's returned text
        else:
            # re-extraction of the value from the page at that step. Session-specific values (currency/date/
            # number) are generalized to a shape regex so the capture replays cross-tenant; anything else keeps
            # its exact escaped literal (ADR-9 note "short/format-variant values may hardcode" now bounded).
            match = _match_capture_pattern(sval)
            if match:
                pattern, desc = match
                _log.info("[capture-pattern] generalized %r value %r to %r (%s)", name, sval, pattern, desc)
            else:
                pattern = re.escape(sval)
            source = CaptureSource(locator=Locator(strategy="css", css="body"),
                                   extract=ExtractSpec(pattern=pattern, **{"from": "text"}))
        captures.append(Capture(name=name, type="string", source_step=step_id, source=source, export=True))
    return captures, dropped, warnings


# ---------------- checkpoints from success_observed_phrases (ADR-005 Gap #1) ----------------

def build_checkpoints(success_phrases: list[str], steps: list[RecordedStep],
                      capture_values: dict[str, str]):
    """Return (checkpoints_by_step_id, warnings). Each phrase is reverse-parameterized against declared
    captures then attached to the step whose observation first showed it (multi-page phrases -> per-step
    checkpoints)."""
    by_step: dict[str, list[str]] = {}
    warnings: list[str] = []
    for phrase in success_phrases:
        step_id = trace_to_step(phrase, steps)             # trace the LITERAL phrase (observations are literal)
        if step_id is None:
            # DROP rather than best-effort-attach: a phrase never seen in any observation window is not
            # replay-verifiable, and attaching it would produce a checkpoint that always fails on replay.
            warnings.append(f"success phrase {phrase!r} not found in any recorded observation; "
                            f"DROPPED from checkpoints (not replay-verifiable)")
            continue
        templated = reverse_parameterize(phrase, capture_values)
        by_step.setdefault(step_id, []).append(templated)
    if success_phrases and not by_step:
        warnings.append("no success phrase was traceable to an observation; artifact has NO checkpoint "
                        "(weak — the auto-replay validation gate should reject it)")
    return by_step, warnings


# ---------------- compose ----------------

def emit_artifact(capability_name: str, steps: list[RecordedStep], finish_result: dict[str, Any],
                  success_observed_phrases: list[str], *, model: str,
                  capability_type: CapabilityType,
                  target_app_hint: Optional[str] = None,
                  caller_parameters: Optional[dict[str, str]] = None,
                  caller_parameter_sources: Optional[dict[str, str]] = None,
                  generated_parameters: Optional[dict[str, str]] = None):
    """Assemble the Artifact (validated=false). Returns (artifact, dropped_exports, warnings).

    caller_parameters (ADR-9): caller-supplied literal values, reverse-parameterized out of every step field
    into {{name}} and declared in `parameters`; recorded in metadata.sample_invocation. generated_parameters:
    a forward seam for fixture generate-markers (declared with generate:unique_string). Both value pools feed
    step-field reverse-parameterization; caller_parameters take priority on a value collision."""
    if not steps:
        raise ValueError("cannot emit an artifact with no recorded steps")

    caller_parameters = caller_parameters or {}
    generated_parameters = generated_parameters or {}
    # caller_parameters first so they win same-value ties (dict order is preserved through the length sort).
    step_rp_values = {**caller_parameters, **generated_parameters}

    actions = [s.action for s in steps]
    capability_type = apply_human_input_override(capability_type, actions)

    captures, dropped, cap_warnings = build_captures(finish_result, steps, caller_parameters)
    capture_values = {c.name: str(finish_result[c.name]) for c in captures}   # only DECLARED captures

    # phrases reverse-parameterize against caller_parameters ∪ captures (ADR-005 "parameters ∪ captures").
    checkpoint_values = {**caller_parameters, **capture_values}
    checkpoints_by_step, cp_warnings = build_checkpoints(success_observed_phrases, steps, checkpoint_values)

    emitted_steps = []
    auto_credentials: set[str] = set()
    for s in steps:
        # reverse-parameterize the caller-supplied literals out of the action's fields (ADR-9)
        action, _ = reverse_parameterize_action(s.action, step_rp_values)
        # force the emitted step's id to the canonical step_id so captures.source_step and checkpoint
        # attachment (both keyed by step_id) resolve against a real step in the model validator.
        updates: dict[str, Any] = {"id": s.step_id}
        # B2 (ADR-010): a hardcoded credential typed into a NAMED credential field is auto-parameterized to a
        # {{canonical}} placeholder + declared sensitive. Only fires when the value is still a literal (a
        # caller/generated value was already templated by reverse_parameterize_action above).
        if action.action == "type_text" and "{{" not in (action.value or ""):
            canonical = detect_credential(*_locator_names(action))
            if canonical:
                updates["value"] = "{{" + canonical + "}}"
                auto_credentials.add(canonical)
                _log.info("[credentials] auto-parameterized %s in step %s", canonical, s.step_id)
        phrases = checkpoints_by_step.get(s.step_id)
        if phrases:
            updates["checkpoint"] = Checkpoint(success=SuccessCriteria(required_phrases=phrases))  # target defaults #rightPanel
        emitted_steps.append(action.model_copy(update=updates))

    # B1 (ADR-010 safety net): after B2, any type_text still holding a credential-SHAPED literal whose field
    # was NOT credential-named escaped auto-parameterization -> refuse to emit rather than persist a possible
    # secret. Values are redacted from the error (never log the secret).
    suspects = [st for st in emitted_steps
                if st.action == "type_text" and looks_like_credential_value(getattr(st, "value", None))]
    if suspects:
        lines = "\n".join(f"    - {st.id}: locator={st.locator.model_dump(exclude_none=True)} "
                          f"value=<redacted, looks like a credential>" for st in suspects)
        raise CredentialsHardcodedError(
            "Refusing to emit artifact: possible hardcoded credentials detected but not auto-parameterized.\n"
            f"Suspect steps:\n{lines}\n"
            "Fix: give the field a credential name/label (e.g. aria-label 'Username'/'Password') so it is "
            "auto-parameterized, OR pass the credential as --caller-params-from-json at discovery time so it "
            "is reverse-parameterized (never stored). (§3.4/§9, ADR-010)")

    parameters = _build_parameters(caller_parameters, generated_parameters, auto_credentials)
    # sample_invocation records the SOURCE of each caller parameter, preferring a JSON registry reference
    # ("$json:dot.path") over the discovery-time literal so the validation gate re-resolves current registry
    # values at replay time. A param with no declared source falls back to its literal (kept non-secret by the
    # B1 CredentialsHardcodedError guard above; the CLI always supplies a $json: source, §3.4/§9).
    sources = caller_parameter_sources or {}
    sample_invocation = ({name: sources.get(name, str(val)) for name, val in caller_parameters.items()}
                         if caller_parameters else None)

    artifact = Artifact(
        version="0.1.0",
        metadata=ArtifactMetadata(capability_name=capability_name, capability_type=capability_type,
                                  validated=False, discovered_by_model=model,
                                  target_app_hint=target_app_hint, sample_invocation=sample_invocation),
        parameters=parameters,
        captures=captures,
        steps=emitted_steps,
    )
    return artifact, dropped, cap_warnings + cp_warnings
