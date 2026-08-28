"""Smoke tests for src/executor.

Deterministic + offline: all page state comes from `set_content` or `page.route` (no live sites — that is
replay/agent's job). Exercises locator resolution (success/fallback/ambiguity/exhaustion), interpolation
(hit/miss), each simple action, all three checkpoint branches, and find_matching. Runs headless for speed.
"""
from __future__ import annotations

import os
import re

import pytest
import pytest_asyncio

from src.executor import (
    InterpolationError,
    LocatorAmbiguityError,
    LocatorResolutionError,
    PlaywrightExecutor,
    VariableScope,
)
from src.executor.interpolation import interpolate
from src.models import (
    Checkpoint,
    ClickAction,
    ExpectedOutcome,
    FindMatchingAction,
    FindMatchingCapture,
    Locator,
    NavigateAction,
    Probe,
    ReadTextAction,
    SuccessCriteria,
    TypeTextAction,
)


@pytest_asyncio.fixture
async def ex(tmp_path):
    executor = PlaywrightExecutor(evidence_dir=tmp_path / "evidence", headless=True)
    await executor.start()
    try:
        yield executor
    finally:
        await executor.stop()


# ---------------- interpolation (sync) ----------------

def test_interpolation_resolves():
    scope = VariableScope(parameters={"amount": 25}, captures={"acct": "13899"})
    assert interpolate("{{amount}} to #{{acct}}", scope, "v") == "25 to #13899"


def test_interpolation_missing_raises():
    with pytest.raises(InterpolationError):
        interpolate("{{nope}}", VariableScope(), "v")


# ---------------- locator resolution + fallback ----------------

async def test_resolve_role_name(ex):
    await ex.page.set_content("<button>Save</button>")
    loc = await ex.resolve_locator(Locator(strategy="role_name", role="button", name="Save"), VariableScope())
    assert await loc.count() == 1
    assert (await loc.inner_text()) == "Save"


async def test_resolve_fallback_chain(ex):
    await ex.page.set_content('<input name="q">')
    locator = Locator(
        strategy="role_name", role="button", name="DoesNotExist",
        fallbacks=[Locator(strategy="css", css='input[name="q"]')],
    )
    loc = await ex.resolve_locator(locator, VariableScope())
    assert await loc.count() == 1


async def test_resolve_ambiguity_fails(ex):
    await ex.page.set_content("<button>Go</button><button>Go</button>")
    with pytest.raises(LocatorAmbiguityError) as ei:
        await ex.resolve_locator(Locator(strategy="role_name", role="button", name="Go"), VariableScope())
    assert ei.value.count == 2  # did NOT silently auto-pick the first


async def test_resolve_exhaustion_captures_screenshot(ex):
    await ex.page.set_content("<div>nothing useful</div>")
    with pytest.raises(LocatorResolutionError) as ei:
        await ex.resolve_locator(Locator(strategy="css", css="#missing"), VariableScope())
    assert ei.value.screenshot_path and os.path.exists(ei.value.screenshot_path)


# ---------------- simple actions ----------------

async def test_click_action(ex):
    await ex.page.set_content(
        "<button onclick=\"document.getElementById('o').innerText='clicked'\">Go</button>"
        "<div id='o'></div>"
    )
    res = await ex.execute_action(
        ClickAction(id="c", locator=Locator(strategy="role_name", role="button", name="Go")),
        VariableScope(),
    )
    assert res.status == "success"
    assert (await ex.page.inner_text("#o")) == "clicked"


async def test_type_text_action(ex):
    await ex.page.set_content('<input id="i">')
    res = await ex.execute_action(
        TypeTextAction(id="t", locator=Locator(strategy="css_id", css="#i"), value="{{v}}"),
        VariableScope(parameters={"v": "hello world"}),
    )
    assert res.status == "success" and res.value == "hello world"
    assert (await ex.page.input_value("#i")) == "hello world"


async def test_read_text_action(ex):
    await ex.page.set_content('<p id="p">Hello World</p>')
    res = await ex.execute_action(
        ReadTextAction(id="r", locator=Locator(strategy="css_id", css="#p")),
        VariableScope(),
    )
    assert res.status == "success" and res.text == "Hello World"


# ---------------- checkpoint: all three branches ----------------

async def test_checkpoint_success_first_poll(ex):
    await ex.page.set_content('<div id="r">Transfer Complete!</div>')
    cp = await ex.resolve_checkpoint(
        Checkpoint(success=SuccessCriteria(required_phrases=["Transfer Complete!"], target="#r")),
        VariableScope(),
    )
    assert cp.status == "success" and cp.polls == 1


async def test_checkpoint_success_after_two_polls(ex):
    await ex.page.set_content('<div id="r"></div>')
    await ex.page.evaluate("setTimeout(() => { document.getElementById('r').innerText = 'DONE'; }, 300)")
    cp = await ex.resolve_checkpoint(
        Checkpoint(success=SuccessCriteria(required_phrases=["DONE"], target="#r"),
                   wait_ms=3000, poll_interval_ms=100),
        VariableScope(),
    )
    assert cp.status == "success" and cp.polls >= 2


async def test_checkpoint_business_outcome(ex):
    await ex.page.set_content('<div id="r">Error! Could not find account # 999999999</div>')
    cp = await ex.resolve_checkpoint(
        Checkpoint(
            success=SuccessCriteria(required_phrases=["Transfer Complete!"], target="#r"),
            expected_outcomes=[ExpectedOutcome(name="account_not_found",
                                               required_phrases=["Could not find account"])],
            wait_ms=400, poll_interval_ms=100,
        ),
        VariableScope(),
    )
    assert cp.status == "business_outcome" and cp.outcome_name == "account_not_found"


async def test_checkpoint_timeout_captures_screenshot(ex):
    await ex.page.set_content('<div id="r">still loading</div>')
    cp = await ex.resolve_checkpoint(
        Checkpoint(success=SuccessCriteria(required_phrases=["NEVER APPEARS"], target="#r"),
                   wait_ms=400, poll_interval_ms=100),
        VariableScope(),
    )
    assert cp.status == "checkpoint_timeout"
    assert cp.screenshot_path and os.path.exists(cp.screenshot_path)


# ---------------- find_matching (matches 2nd of 3 candidates) ----------------

async def test_find_matching_second_candidate(ex):
    async def handler(route):
        m = re.search(r"id=(\w+)", route.request.url)
        cid = m.group(1) if m else ""
        body = "Account Type: SAVINGS" if cid == "2" else "Account Type: CHECKING"
        await route.fulfill(status=200, content_type="text/html",
                            body=f"<html><body>{body}</body></html>")

    await ex.page.route(re.compile(r"://acct\.test/acct"), handler)

    scope = VariableScope(captures={"accts": ["1", "2", "3"]})
    action = FindMatchingAction(
        id="fm",
        candidates="accts",
        probe=Probe(
            action="navigate",
            locator=Locator(strategy="url_template", url_template="http://acct.test/acct?id={{candidate}}"),
            checkpoint=Checkpoint(success=SuccessCriteria(required_phrases=["SAVINGS"], target="body"),
                                  wait_ms=800, poll_interval_ms=100),
        ),
        capture=FindMatchingCapture(variable="savings_id", value_from="candidate"),
    )
    res = await ex.execute_action(action, scope)
    assert res.status == "success"
    assert res.bound_value == "2" and res.candidates_tried == 2
    assert scope.captures["savings_id"] == "2"
    # the reserved `candidate` must NOT leak into the caller's scope
    assert "candidate" not in scope.captures


async def test_find_matching_exhausted(ex):
    async def handler(route):
        await route.fulfill(status=200, content_type="text/html",
                            body="<html><body>Account Type: CHECKING</body></html>")

    await ex.page.route(re.compile(r"://acct\.test/acct"), handler)
    scope = VariableScope(captures={"accts": ["1", "2"]})
    action = FindMatchingAction(
        id="fm",
        candidates="accts",
        probe=Probe(
            action="navigate",
            locator=Locator(strategy="url_template", url_template="http://acct.test/acct?id={{candidate}}"),
            checkpoint=Checkpoint(success=SuccessCriteria(required_phrases=["SAVINGS"], target="body"),
                                  wait_ms=300, poll_interval_ms=100),
        ),
        capture=FindMatchingCapture(variable="savings_id", value_from="candidate"),
    )
    res = await ex.execute_action(action, scope)
    assert res.status == "find_matching_exhausted"
    assert res.candidates_tried == 2
    assert res.screenshot_path and os.path.exists(res.screenshot_path)
    assert "savings_id" not in scope.captures
