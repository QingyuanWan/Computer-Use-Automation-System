"""Translate a models.Locator into a concrete Playwright locator (ADR-002; schema-draft §6).

Resolution honors the ordered fallback chain and `{{name}}` interpolation, with strict match semantics:
  - N == 1  -> resolved, return it
  - N == 0  -> try the next fallback
  - N  > 1  -> LocatorAmbiguityError (refuse to auto-pick — silent-failure category, task item 2)
  - all attempts N == 0 -> LocatorResolutionError with the last page's ARIA snapshot for debugging

No Playwright-specific syntax lives in the artifact; it is synthesized here (the ADR-002 "seam").
"""
from __future__ import annotations

import json
import logging

from .evidence import EvidenceCapture
from .interpolation import interpolate
from .results import ExecutorError, LocatorAmbiguityError, LocatorResolutionError, VariableScope

_log = logging.getLogger("executor.locator")


def _build_pw_locator(page, loc, scope: VariableScope):
    """Construct (but do not count) a Playwright locator from one models.Locator. Interpolates every
    string field. `strategy` is advisory: if absent we infer from the fields that are present (schema-draft
    §11 item 18)."""
    strat = loc.strategy.value if loc.strategy is not None else None

    def s(field_name, value):
        return interpolate(value, scope, field=f"locator.{field_name}")

    if strat == "role_name" or (strat is None and loc.role and loc.name):
        name = s("name", loc.name)
        base = page.get_by_role(loc.role, name=name) if name else page.get_by_role(loc.role)
    elif strat == "role_nth" or (strat is None and loc.role and loc.index is not None):
        base = page.get_by_role(loc.role)
    elif strat in ("css", "css_id") or (strat is None and loc.css):
        base = page.locator(s("css", loc.css))
    elif strat == "id" or (strat is None and loc.id):
        # use [id="..."] not #id so dotted ids (e.g. customer.username) don't parse as CSS class selectors
        base = page.locator(f'[id={json.dumps(s("id", loc.id))}]')
    elif strat == "href_pattern" or (strat is None and loc.href_pattern):
        base = page.locator(f'a[href*={json.dumps(s("href_pattern", loc.href_pattern))}]')
    elif strat == "text" or (strat is None and loc.text):
        base = page.get_by_text(s("text", loc.text))
    elif strat == "label" or (strat is None and loc.label):
        base = page.get_by_label(s("label", loc.label))
    elif strat == "placeholder" or (strat is None and loc.placeholder):
        base = page.get_by_placeholder(s("placeholder", loc.placeholder))
    elif strat == "url_template":
        # url_template locators are consumed by navigate (find_matching.probe), not resolved as elements.
        raise ExecutorError("url_template locator is for navigation, not element resolution")
    else:
        raise ExecutorError(f"cannot build a Playwright locator from {loc!r}")

    if loc.index is not None:
        base = base.nth(loc.index)
    return base


def build_pw_locator(page, loc, scope: VariableScope):
    """Public: build (do not count) a Playwright locator from a models.Locator. Used for screenshot masking
    (§3.4), where we want the locator handle without the strict N==1 resolution `resolve_locator` enforces."""
    return _build_pw_locator(page, loc, scope)


async def _safe_aria(page) -> str:
    try:
        return await page.locator("body").aria_snapshot()
    except Exception:
        return "<aria snapshot unavailable>"


async def resolve_locator(page, locator, scope: VariableScope, evidence: EvidenceCapture):
    """Resolve `locator` (primary + ordered fallbacks) to exactly one Playwright element.

    Raises LocatorAmbiguityError (N>1) or LocatorResolutionError (all N==0), each carrying an evidence
    screenshot path (ADR-004). InterpolationError propagates unchanged.
    """
    attempts = [locator, *locator.fallbacks]
    for i, loc in enumerate(attempts):
        try:
            pw = _build_pw_locator(page, loc, scope)
        except ExecutorError as exc:
            # a build failure (e.g. unusable fields) is treated like a miss -> try the next fallback
            _log.info("locator attempt %d (strategy=%s) could not be built: %s", i, loc.strategy, exc)
            continue
        count = await pw.count()
        _log.info("locator attempt %d strategy=%s -> %d match(es)", i, loc.strategy, count)
        if count == 1:
            return pw
        if count > 1:
            shot = await evidence.capture("locator_ambiguity")
            raise LocatorAmbiguityError(
                f"locator attempt {i} (strategy={loc.strategy}) matched {count} elements; "
                f"refusing to auto-pick (add an index/nth or a more specific fallback)",
                count=count, strategy=str(loc.strategy), screenshot_path=shot,
            )
        # count == 0 -> next fallback

    aria = await _safe_aria(page)
    shot = await evidence.capture("locator_exhausted")
    raise LocatorResolutionError(
        f"all {len(attempts)} locator attempt(s) matched 0 elements",
        aria=aria, screenshot_path=shot,
    )
