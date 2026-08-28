"""Smoke tests for src/replay.

Static HTML via `set_content`/`page.route` (no live sites); artifacts built as Pydantic models in-test.
Exercises all three checkpoint branches (success/business_outcome/hard_failure), parameter interpolation,
capture binding + downstream reference, generate-marker synthesis, fixture composition + failure, the ADR-7
resume/takeover_resume distinction, and the two-level-depth guard.
"""
from __future__ import annotations

import os
import re

import pytest
import pytest_asyncio

from src.executor import PlaywrightExecutor
from src.models import (
    Artifact,
    ArtifactMetadata,
    Capture,
    CaptureSource,
    Checkpoint,
    ClickAction,
    ExpectedOutcome,
    ExtractSpec,
    Locator,
    NavigateAction,
    Parameter,
    ParametersBlock,
    ReadTextAction,
    SuccessCriteria,
    TypeTextAction,
)
from src.replay import (
    EscalationHandler,
    EscalationOutcome,
    ReplayEngine,
    StubEscalationHandler,
)


@pytest_asyncio.fixture
async def ex(tmp_path):
    executor = PlaywrightExecutor(evidence_dir=tmp_path / "evidence", headless=True)
    await executor.start()
    try:
        yield executor
    finally:
        await executor.stop()


def _no_loader(name):
    raise AssertionError(f"unexpected fixture load: {name!r}")


def _engine(ex, handler=None, loader=None):
    return ReplayEngine(executor=ex, escalation_handler=handler or StubEscalationHandler(),
                        artifact_loader=loader or _no_loader)


async def _route(ex, pattern, html):
    async def handler(route):
        await route.fulfill(status=200, content_type="text/html", body=html)
    await ex.page.route(re.compile(pattern), handler)


def _meta(**kw):
    kw.setdefault("capability_type", "read")
    return ArtifactMetadata(capability_name="test", **kw)


# ---------------- happy path: navigate + read_text(capture) + export -> success ----------------

async def test_happy_path_success(ex):
    await _route(ex, r"://app\.test/", '<div id="acct">12345</div>')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        captures=[Capture(name="acct_id", type="string", source_step="read_acct", export=True)],
        steps=[
            NavigateAction(id="go", url="http://app.test/overview"),
            ReadTextAction(id="read_acct", locator=Locator(strategy="css_id", css="#acct"),
                           checkpoint=Checkpoint(success=SuccessCriteria(required_phrases=["12345"], target="#acct"),
                                                 wait_ms=1000, poll_interval_ms=100)),
        ],
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "success"
    assert res.outputs == {"acct_id": "12345"}


# ---------------- business_outcome (short-circuits remaining steps) ----------------

async def test_business_outcome_short_circuits(ex):
    await ex.page.set_content('<div id="r">Error! Could not find account # 999999999</div>')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        steps=[
            ReadTextAction(
                id="s1", locator=Locator(strategy="css_id", css="#r"),
                checkpoint=Checkpoint(
                    success=SuccessCriteria(required_phrases=["Account Details"], target="#r"),
                    expected_outcomes=[ExpectedOutcome(name="account_not_found",
                                                       required_phrases=["Could not find account"])],
                    wait_ms=400, poll_interval_ms=100),
            ),
            # would raise LocatorResolutionError if ever executed -> proves short-circuit
            ClickAction(id="s2_never", locator=Locator(strategy="css_id", css="#does_not_exist")),
        ],
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "business_outcome"
    assert res.outcome_name == "account_not_found"


# ---------------- hard_failure via stub escalation (abort) ----------------

async def test_hard_failure_stub_abort(ex):
    await ex.page.set_content('<div id="r">still loading</div>')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#r"),
                              checkpoint=Checkpoint(success=SuccessCriteria(required_phrases=["NEVER APPEARS"], target="#r"),
                                                    wait_ms=300, poll_interval_ms=100))],
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "hard_failure"
    assert res.failed_step_id == "s1"
    assert res.screenshot_path and os.path.exists(res.screenshot_path)


# ---------------- parameter interpolation ----------------

async def test_parameter_interpolation(ex):
    await ex.page.set_content('<input id="i">')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        parameters=ParametersBlock(properties={"amount": Parameter(type="string")}, required=["amount"]),
        steps=[TypeTextAction(id="t", locator=Locator(strategy="css_id", css="#i"), value="{{amount}}")],
    )
    res = await _engine(ex).replay(art, {"amount": "42"})
    assert res.status == "success"
    assert (await ex.page.input_value("#i")) == "42"


# ---------------- capture binding + downstream reference ----------------

async def test_capture_downstream_reference(ex):
    await ex.page.set_content('<div id="src">detailpage</div><input id="dst">')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        captures=[Capture(name="target", type="string", source_step="s1")],   # intermediate, not exported
        steps=[
            ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#src")),
            TypeTextAction(id="s2", locator=Locator(strategy="css_id", css="#dst"), value="{{target}}"),
        ],
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "success"
    assert (await ex.page.input_value("#dst")) == "detailpage"


# ---------------- generate marker ----------------

async def test_generate_marker(ex):
    await ex.page.set_content('<input id="i">')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        parameters=ParametersBlock(
            properties={"user": Parameter(type="string", generate="unique_string", export=True)},
            required=["user"]),
        steps=[TypeTextAction(id="t", locator=Locator(strategy="css_id", css="#i"), value="{{user}}")],
    )
    res = await _engine(ex).replay(art, {})
    typed = await ex.page.input_value("#i")
    assert res.status == "success"
    assert res.outputs["user"] == typed
    assert re.fullmatch(r"user_\d{14}_[0-9a-f]{4}", typed), typed   # per-run <base>_<ts>_<4hex> (schema §3)


# (fixture composition removed per ADR-9 — validation uses caller_parameters/sample_invocation instead.)


# ---------------- ADR-7 resume: re-poll fresh budget after human "waits" ----------------

class _MutatingHandler(EscalationHandler):
    def __init__(self, page, action):
        self._page = page
        self._action = action
        self.calls = 0

    async def escalate(self, context):
        self.calls += 1
        await self._page.evaluate("document.getElementById('r').innerText = 'DONE'")  # human resolves it
        return EscalationOutcome(action=self._action, operator_note="handled")


async def test_escalation_resume_repolls(ex):
    await ex.page.set_content('<div id="r">loading</div>')
    handler = _MutatingHandler(ex.page, "resume")
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#r"),
                              checkpoint=Checkpoint(success=SuccessCriteria(required_phrases=["DONE"], target="#r"),
                                                    wait_ms=200, poll_interval_ms=100))],
    )
    res = await _engine(ex, handler=handler).replay(art, {})
    assert res.status == "success"
    assert handler.calls == 1


async def test_escalation_takeover_resume_one_shot(ex):
    await ex.page.set_content('<div id="r">loading</div>')
    handler = _MutatingHandler(ex.page, "takeover_resume")
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#r"),
                              checkpoint=Checkpoint(success=SuccessCriteria(required_phrases=["DONE"], target="#r"),
                                                    wait_ms=200, poll_interval_ms=100))],
    )
    res = await _engine(ex, handler=handler).replay(art, {})
    assert res.status == "success"
    assert handler.calls == 1


# (two-level fixture-depth guard removed per ADR-9 — fixture composition is no longer the gate mechanism.)


# ---------------- source-based capture: single-value regex extraction ----------------

async def test_source_extract_single_value(ex):
    await ex.page.set_content('<a id="lnk" href="activity.htm?id=777">detail</a>')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        captures=[Capture(name="acct", type="string", source_step="s1", export=True,
                          source=CaptureSource(locator=Locator(strategy="css_id", css="#lnk"),
                                               extract=ExtractSpec(pattern=r"id=(\d+)", **{"from": "href"})))],
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="body"))],
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "success"
    assert res.outputs == {"acct": "777"}


# ---------------- expected_outcomes evaluated when the success capture fails (bug fix) ----------------

def _fast_capture(monkeypatch):
    import src.replay.engine as eng
    monkeypatch.setattr(eng, "_CAPTURE_WAIT_MS", 300)   # don't wait the full 5s on a genuine capture miss
    monkeypatch.setattr(eng, "_CAPTURE_POLL_MS", 100)


def _acct_step(with_business=True, wait_ms=400):
    outcomes = ([ExpectedOutcome(name="account_not_found",
                                 required_phrases=["Could not find account # 999999999"])]
                if with_business else [])
    return ReadTextAction(
        id="s1", locator=Locator(strategy="css_id", css="#r"),
        checkpoint=Checkpoint(
            success=SuccessCriteria(required_phrases=["Account Details", "{{account_type}}"], target="#r"),
            expected_outcomes=outcomes, wait_ms=wait_ms, poll_interval_ms=100))


def _acct_artifact(step):
    return Artifact(
        version="0.1.0", metadata=_meta(),
        captures=[Capture(name="account_type", type="string", source_step="s1", export=True,
                          source=CaptureSource(locator=Locator(strategy="css_id", css="#r"),
                                               extract=ExtractSpec(pattern=r"(CHECKING|SAVINGS)",
                                                                   **{"from": "text"})))],
        steps=[step])


async def test_capture_failure_with_business_outcome_returns_business(ex, monkeypatch):
    # THE BUG: success capture 'account_type' (pattern CHECKING/SAVINGS) is absent because the page reached a
    # recognized business outcome. Must classify business_outcome(account_not_found), NOT technical_error.
    _fast_capture(monkeypatch)
    await ex.page.set_content('<div id="r">Error! Could not find account # 999999999</div>')
    res = await _engine(ex).replay(_acct_artifact(_acct_step(with_business=True)), {})
    assert res.status == "business_outcome"
    assert res.outcome_name == "account_not_found"


async def test_success_wins_when_capture_ok_and_business_would_also_match(ex):
    # page carries BOTH the success value (CHECKING) AND a business phrase; capture succeeds -> success wins,
    # business_outcome must not misfire (resolve_checkpoint checks success before expected_outcomes).
    await ex.page.set_content(
        '<div id="r">Account Details Account Type: CHECKING Balance: $10 '
        'Could not find account # 999999999</div>')
    res = await _engine(ex).replay(_acct_artifact(_acct_step(with_business=True)), {})
    assert res.status == "success"
    assert res.outputs == {"account_type": "CHECKING"}


async def test_capture_failure_no_matching_business_is_technical(ex, monkeypatch):
    # capture fails AND no declared business outcome matches -> genuine technical_error (not masked)
    _fast_capture(monkeypatch)
    await ex.page.set_content('<div id="r">some unexpected broken state</div>')
    res = await _engine(ex).replay(_acct_artifact(_acct_step(with_business=True)), {})
    assert res.status == "hard_failure"
    assert (res.reason or "").startswith("technical_error")


async def test_capture_failure_no_checkpoint_is_technical(ex, monkeypatch):
    # a step with a capture but NO checkpoint keeps capture-only semantics: a miss is technical, not business
    _fast_capture(monkeypatch)
    await ex.page.set_content('<div id="r">nothing useful here</div>')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        captures=[Capture(name="account_type", type="string", source_step="s1", export=True,
                          source=CaptureSource(locator=Locator(strategy="css_id", css="#r"),
                                               extract=ExtractSpec(pattern=r"(CHECKING|SAVINGS)",
                                                                   **{"from": "text"})))],
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#r"))],   # no checkpoint
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "hard_failure"
    assert (res.reason or "").startswith("technical_error")


# ---------------- source-based capture: polls for an async-rendered value ----------------

async def test_source_extract_polls_for_async_value(ex):
    # the value is empty at first and populated ~600ms later (like ParaBank's AJAX detail fields); the
    # capture-binding poll must wait for it instead of losing the race (capture-async-render fix).
    await ex.page.set_content('<div id="d">Balance: </div>')
    await ex.page.evaluate(
        "setTimeout(() => { document.getElementById('d').innerText = 'Balance: $415.50'; }, 600)")
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        captures=[Capture(name="bal", type="string", source_step="s1", export=True,
                          source=CaptureSource(locator=Locator(strategy="css_id", css="#d"),
                                               extract=ExtractSpec(pattern=r"Balance: (\$[0-9.]+)",
                                                                   **{"from": "text"})))],
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="#d"))],
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "success"
    assert res.outputs == {"bal": "$415.50"}


# ---------------- source-based capture: list extraction (extract.all) ----------------

async def test_source_extract_all_list(ex):
    await ex.page.set_content(
        '<a href="activity.htm?id=11">a</a><a href="activity.htm?id=22">b</a>'
        '<a href="activity.htm?id=33">c</a>'
    )
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        captures=[Capture(name="ids", type="string[]", source_step="s1", export=True,
                          source=CaptureSource(
                              locator=Locator(strategy="href_pattern", href_pattern="activity.htm?id="),
                              extract=ExtractSpec(pattern=r"id=(\d+)", all=True, **{"from": "href"})))],
        steps=[ReadTextAction(id="s1", locator=Locator(strategy="css_id", css="body"))],
    )
    res = await _engine(ex).replay(art, {})
    assert res.status == "success"
    assert res.outputs == {"ids": ["11", "22", "33"]}


# ---------------- locator-fail escalation -> stub abort -> hard_failure ----------------

async def test_locator_failure_escalates_to_hard_failure(ex):
    await ex.page.set_content('<div>no matching element here</div>')
    art = Artifact(
        version="0.1.0", metadata=_meta(),
        steps=[ClickAction(id="s1", locator=Locator(strategy="css_id", css="#definitely-missing"))],
    )
    res = await _engine(ex).replay(art, {})   # StubEscalationHandler aborts
    assert res.status == "hard_failure"
    assert res.failed_step_id == "s1"
    assert res.screenshot_path and os.path.exists(res.screenshot_path)
