"""Failure-injection sub-run (ADR-005).

For a `capability_type: read` capability, after the happy-path discovery succeeds we deliberately re-invoke
discovery with a few invalid inputs to author `expected_outcomes` (named business-outcome branches). Each
injection result is admitted only if it passes a 3-condition classifier (all three, not short-circuit):

    (1) the final page returned HTTP 200 (a rendered business page, not a 4xx/5xx hard error)
    (2) the injected value is echoed back in the final ARIA text (we actually reached the failure page)
    (3) the final URL is NOT a generic error/redirect route (we stayed on the capability's page)

Passing observations become an ExpectedOutcome named after the strategy; the failure phrase is
reverse-parameterized with the SAME guardrails as success phrases (ADR-005 Gap #2). Write/mutating
capabilities skip injection entirely (destructive-injection guard) — enforced by the caller BEFORE any
injection action fires.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import dataclass

from src.models import CapabilityType, ExpectedOutcome

from .emission import reverse_parameterize

_log = logging.getLogger("agent.failure_injection")


@dataclass(frozen=True)
class InjectionStrategy:
    name: str                 # -> ExpectedOutcome.name
    injected_value: str       # the invalid value we steer the LLM to submit / look up
    goal_hint: str            # appended to the base goal to steer toward triggering the failure


# Conservative starter set (ADR-005 says 2-3 is fine; more can be added by a later ADR).
STRATEGIES: list[InjectionStrategy] = [
    InjectionStrategy(
        name="account_not_found",
        injected_value="999999999",
        goal_hint=("IMPORTANT OVERRIDE: instead of the real account, look up account number 999999999 "
                   "(which does not exist). Do NOT give up — navigate to the account's activity/detail page "
                   "and report EXACTLY the message the site shows."),
    ),
    InjectionStrategy(
        name="malformed_account_rejected",
        injected_value="notanumber",
        goal_hint=("IMPORTANT OVERRIDE: instead of the real account, look up the account whose id is the text "
                   "'notanumber' (letters, not a valid id). Navigate to that account's activity/detail page "
                   "and report EXACTLY the message the site shows."),
    ),
]


def _is_generic_error_route(url: str) -> bool:
    """Heuristic for condition (3): did we get bounced to a generic error/login route (vs. the capability's
    own page rendering a business message in place)? ParaBank renders 'Could not find account' in-place on
    activity.htm, so that is NOT a generic error route."""
    path = urllib.parse.urlparse(url).path.lower()
    return path.endswith("/error.htm") or path.endswith("/login.htm") or "/error" in path


async def _http_status(page) -> "int | None":
    """Final-page HTTP status via the Navigation Timing API (Chromium exposes responseStatus). None if it
    cannot be determined."""
    try:
        return await page.evaluate(
            "() => { const e = performance.getEntriesByType('navigation'); "
            "return e && e[0] ? (e[0].responseStatus || null) : null; }")
    except Exception:  # noqa: BLE001 - a missing/older API must not crash the sub-run
        return None


def _failure_phrase(aria: str, injected_value: str) -> "str | None":
    """The single most descriptive line of the final ARIA that echoes the injected value (the failure
    message). Prefers a quoted accessible-name; falls back to the text after a `role:` prefix. URL lines are
    ignored so a value that only appears in an href is not mistaken for a visible message."""
    best: "str | None" = None
    for line in aria.splitlines():
        if injected_value not in line or "/url:" in line:
            continue
        m = re.search(r'"([^"]*)"', line)
        if m and injected_value in m.group(1):
            text = m.group(1)
        else:
            text = re.sub(r'^\s*-\s*(?:[\w-]+:)?\s*', '', line).strip().strip('"')
        if injected_value in text and (best is None or len(text) > len(best)):
            best = text
    return best


async def classify_injection(page, injected_value: str, capture_values: dict[str, str]):
    """Apply the 3-condition classifier to the CURRENT page. Returns (admitted, phrases). All three
    conditions are evaluated (no optimistic short-circuit) so the debug log shows exactly which failed."""
    status = await _http_status(page)
    aria = await page.locator("body").aria_snapshot()
    url = page.url

    cond_http_200 = (status is None) or (status == 200)     # unknown status is lenient; a real 5xx fails
    cond_value_echoed = injected_value in aria
    cond_same_route = not _is_generic_error_route(url)

    if not (cond_http_200 and cond_value_echoed and cond_same_route):
        _log.debug("injection discarded: http200=%s value_echoed=%s same_route=%s url=%s status=%s",
                   cond_http_200, cond_value_echoed, cond_same_route, url, status)
        return False, []

    phrase = _failure_phrase(aria, injected_value)
    if phrase is None:
        _log.debug("injection discarded: value in ARIA but no visible failure phrase (url=%s)", url)
        return False, []
    phrases = [reverse_parameterize(phrase, capture_values)]     # same guardrails as success phrases
    return True, phrases


async def run_failure_injection(agent, artifact, base_goal: str, target_url: str, capability_name: str,
                                *, caller_parameters: "dict[str, str] | None" = None):
    """Run every strategy through the discovery agent, classify each final page, and return
    (expected_outcomes, per_run_usages, warnings). Only invoked for read capabilities (caller-enforced).

    caller_parameters (ADR-9) are forwarded to each injection sub-run so the OTHER params (e.g. login
    credentials) stay parameterized and the login still works; the strategy's injected invalid value is NOT
    a caller parameter, so it is never reverse-parameterized. They also seed the classifier's failure-phrase
    reverse-parameterization (a real caller value appearing in the failure text is templated; the injected
    value stays literal)."""
    caller_parameters = caller_parameters or {}
    outcomes: list[ExpectedOutcome] = []
    usages: list[dict[str, int]] = []
    warnings: list[str] = []

    for strat in STRATEGIES:
        goal = f"{base_goal}\n\n{strat.goal_hint}"
        # generate_hints=False: injection sub-run artifacts are discarded (only their expected_outcomes are
        # harvested), so they must not pay for the secondary hint LLM call (ADR-007 revision cost guard).
        sub = await agent.discover(goal, target_url, f"{capability_name}__inject_{strat.name}",
                                   caller_parameters, capability_type=CapabilityType.read,
                                   generate_hints=False)
        usages.append(sub.usage)
        if sub.status != "success":
            warnings.append(f"injection[{strat.name}]: sub-run did not finish (status={sub.status}); discarded")
            continue
        admitted, phrases = await classify_injection(agent.executor.page, strat.injected_value,
                                                     caller_parameters)
        if admitted:
            outcomes.append(ExpectedOutcome(name=strat.name, required_phrases=phrases))
            _log.info("injection[%s] admitted expected_outcome: %s", strat.name, phrases)
        else:
            warnings.append(f"injection[{strat.name}]: discarded by 3-condition classifier")
    return outcomes, usages, warnings
