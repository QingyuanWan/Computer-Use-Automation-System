"""Translate LLM tool arguments into strict models actions.

The LLM emits loose locator dicts (e.g. {role, nth}); models.Locator is strict (extra=forbid). This layer
normalizes: maps `nth` -> `index`, drops unrecognized keys, infers `strategy` from present fields, and
recurses into fallbacks. Building the Pydantic model validates it; failures become TranslationError so the
loop records the miss and continues.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from src.models import (
    Checkpoint,
    ClickAction,
    FindMatchingAction,
    FindMatchingCapture,
    Locator,
    NavigateAction,
    Probe,
    ReadTextAction,
    SuccessCriteria,
    TypeTextAction,
)

from .results import TranslationError

_LOCATOR_FIELDS = {"role", "name", "href_pattern", "css", "id", "text", "index",
                   "label", "placeholder", "url_template", "strategy"}


def _infer_strategy(f: dict[str, Any]) -> str | None:
    if f.get("role") and f.get("name"):
        return "role_name"
    if f.get("role") and f.get("index") is not None:
        return "role_nth"
    if f.get("href_pattern"):
        return "href_pattern"
    if f.get("id"):
        return "id"
    if f.get("css"):
        return "css_id"
    if f.get("url_template"):
        return "url_template"
    if f.get("text"):
        return "text"
    if f.get("label"):
        return "label"
    if f.get("placeholder"):
        return "placeholder"
    if f.get("role"):
        return "role_name"
    return None


def to_locator(raw: Any) -> Locator:
    if not isinstance(raw, dict):
        raise TranslationError(f"locator is not an object: {raw!r}")
    d = dict(raw)
    if "nth" in d and "index" not in d:      # LLM says nth; model field is index
        d["index"] = d.pop("nth")
    raw_fallbacks = d.pop("fallbacks", None) or []
    fields = {k: v for k, v in d.items() if k in _LOCATOR_FIELDS}
    if "strategy" not in fields:
        strat = _infer_strategy(fields)
        if strat is not None:
            fields["strategy"] = strat
    try:
        return Locator(**fields, fallbacks=[to_locator(fb) for fb in raw_fallbacks])
    except ValidationError as exc:
        raise TranslationError(f"invalid locator {raw!r}: {exc}") from exc


def _to_checkpoint(raw: dict[str, Any]) -> Checkpoint:
    success = raw["success"]
    return Checkpoint(success=SuccessCriteria(required_phrases=list(success["required_phrases"]),
                                              target=success.get("target", "#rightPanel")))


def to_action(name: str, args: dict[str, Any], step_id: str):
    """Build the models action for an executor tool call. Raises TranslationError on malformed args."""
    try:
        if name == "navigate":
            return NavigateAction(id=step_id, url=args["url"])
        if name == "click":
            return ClickAction(id=step_id, locator=to_locator(args["locator"]))
        if name == "type_text":
            return TypeTextAction(id=step_id, locator=to_locator(args["locator"]), value=args["value"])
        if name == "read_text":
            return ReadTextAction(id=step_id, locator=to_locator(args["locator"]))
        if name == "find_matching":
            probe = args["probe"]
            capture = args["capture"]
            return FindMatchingAction(
                id=step_id, candidates=args["candidates"],
                probe=Probe(action=probe["action"], locator=to_locator(probe["locator"]),
                            checkpoint=_to_checkpoint(probe["checkpoint"])),
                capture=FindMatchingCapture(variable=capture["variable"],
                                            value_from=capture.get("value_from", "candidate")))
    except (KeyError, ValidationError) as exc:
        raise TranslationError(f"invalid {name} args {args!r}: {exc}") from exc
    raise TranslationError(f"not an executor action: {name!r}")
