"""Escalation-hint generation (ADR-007 revision, D1-α / D2-β).

A SECONDARY, emission-time LLM call — deliberately separate from the goal-driven discovery loop. The
discovery main loop's system prompt and tool schema are UNCHANGED and never expose `step_intents`, so the
loop stays 100% goal-driven (§3.1). Hints are authored AFTER a capability is discovered, from a compact
summary of the emitted steps (NOT the observation history — cost stays bounded, ~$0.05).

Pipeline:
  1. `is_hint_worthy(step)`  (D2-β) — which emitted steps deserve a hint.
  2. `generate_step_hints(...)` (async, LLM) — one short operator-facing sentence per hint-worthy step.
  3. `attach_hints(artifact, raw, param_values)` (pure) — reverse-parameterize each hint (so session values
     become {{name}} — reuses the ADR-9 pass) and attach to `step.metadata.escalation_hint`.

Steps 1 + 3 are pure and unit-tested without the LLM; step 2 is tested with a mock client.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from src.models import Artifact, StepMetadata

from .emission import reverse_parameterize

_log = logging.getLogger("agent.hints")

# D2-β: hints only for these action types, PLUS any step that carries a checkpoint (checkpoint failure is the
# most common reactive-escalation trigger, so the operator most needs context there). Simple type_text / click
# / read_text are self-evident from their locator and get no stored hint.
HINT_WORTHY_ACTIONS = frozenset({"navigate", "find_matching", "human_input"})

_MAX_HINT_TOKENS = 600   # bounded output: a handful of one-sentence hints (cost guard, contract item 2)

_SYSTEM = (
    "You annotate the steps of an already-recorded web-automation capability with short operator hints. "
    "A human operator may have to take over mid-replay; each hint is ONE plain sentence telling them what "
    "that step is trying to accomplish, in business terms. Rules: (a) <= 1 sentence, no trailing period "
    "needed; (b) describe intent ('Open the checking account's activity page to read its balance'), NOT "
    "mechanics ('click the third link'); (c) NEVER invent specific account numbers, dollar amounts, user "
    "ids or dates — if a step already contains a {{placeholder}}, keep it verbatim; (d) reply with ONLY a "
    "JSON object mapping step_id -> hint, no prose, no code fence."
)


def is_hint_worthy(step: Any) -> bool:
    """D2-β predicate over an EMITTED step (has `.action` and `.checkpoint`)."""
    return step.action in HINT_WORTHY_ACTIONS or getattr(step, "checkpoint", None) is not None


def _locator_gist(step: Any) -> Optional[str]:
    loc = getattr(step, "locator", None)
    if loc is None:
        return None
    parts = []
    for f in ("role", "name", "text", "href_pattern", "css", "id"):
        v = getattr(loc, f, None)
        if v:
            parts.append(f"{f}={v}")
    return ", ".join(parts) if parts else None


def _summarize_step(step: Any) -> dict[str, Any]:
    """A compact, bounded descriptor of one emitted step for the hint prompt (no observation history)."""
    d: dict[str, Any] = {"id": step.id, "action": step.action}
    if step.action == "navigate":
        d["url"] = step.url
    elif step.action == "find_matching":
        d["selects_from"] = step.candidates
    elif step.action == "human_input":
        d["prompt"] = step.prompt
        d["reason"] = step.reason
    gist = _locator_gist(step)
    if gist:
        d["target"] = gist
    if getattr(step, "checkpoint", None) is not None:
        d["verifies"] = list(step.checkpoint.success.required_phrases)
    return d


def _parse_hints(text: str) -> dict[str, str]:
    """Parse the model's JSON reply into a {step_id: hint} dict. Tolerant of an accidental code fence; a
    non-JSON reply yields {} (hints are best-effort — a parse failure must not fail discovery)."""
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if "\n" in body:
            body = body.split("\n", 1)[1]
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        _log.warning("[hints] could not parse hint JSON; emitting artifact without hints")
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): str(v).strip() for k, v in obj.items() if isinstance(v, str) and v.strip()}


async def generate_step_hints(client: Any, model: str, artifact: Artifact, goal: str,
                              target_url: str) -> "tuple[dict[str, str], Any]":
    """Secondary LLM call. Returns (raw_hints, usage). Only hint-worthy steps are sent (bounded prompt). On any
    error, returns ({}, None) so discovery still emits a (hint-less) artifact — hints are an enhancement."""
    worthy = [s for s in artifact.steps if is_hint_worthy(s)]
    if not worthy:
        return {}, None
    summary = [_summarize_step(s) for s in worthy]
    user = (f"GOAL: {goal}\nTARGET: {target_url}\n\n"
            f"Steps needing a hint (write a hint for EACH id):\n{json.dumps(summary, indent=2)}")
    try:
        resp = await client.messages.create(
            model=model, max_tokens=_MAX_HINT_TOKENS,
            system=_SYSTEM, messages=[{"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001 - hints are best-effort; never fail discovery over them
        _log.warning("[hints] hint-generation LLM call failed (%s); emitting artifact without hints", exc)
        return {}, None
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
    hints = _parse_hints(text)
    # only keep hints for steps we actually asked about (guard against hallucinated ids)
    worthy_ids = {s.id for s in worthy}
    hints = {k: v for k, v in hints.items() if k in worthy_ids}
    _log.info("[hints] generated %d/%d step hints", len(hints), len(worthy))
    return hints, getattr(resp, "usage", None)


def attach_hints(artifact: Artifact, raw_hints: dict[str, str],
                 param_values: dict[str, str]) -> Artifact:
    """PURE. Reverse-parameterize each hint (session values -> {{name}}, reusing the ADR-9 pass) and attach it
    to the step's `metadata.escalation_hint`. Hints for non-hint-worthy or unknown steps are ignored. An empty
    `raw_hints` returns the artifact unchanged (backward compat: emission works with no hint call)."""
    if not raw_hints:
        return artifact
    new_steps = []
    changed = False
    for s in artifact.steps:
        hint = raw_hints.get(s.id, "").strip()
        if hint and is_hint_worthy(s):
            hint = reverse_parameterize(hint, param_values) if param_values else hint
            existing = s.metadata.model_dump() if s.metadata else {}
            new_steps.append(s.model_copy(
                update={"metadata": StepMetadata(**{**existing, "escalation_hint": hint})}))
            changed = True
        else:
            new_steps.append(s)
    return artifact.model_copy(update={"steps": new_steps}) if changed else artifact
