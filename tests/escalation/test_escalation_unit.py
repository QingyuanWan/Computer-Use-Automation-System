"""Phase-A unit tests for src/escalation — all mock-based, no real browser launch.

A FakePage records add_init_script / expose_binding / evaluate and lets tests resolve an escalation by
invoking the registered `resumeAutomation` binding directly (what a real button click would do).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.escalation import (
    PANEL_JS_SOURCE,
    PlaywrightEscalationHandler,
    ResumeBridge,
    summarize,
)
from src.escalation.dom_diff import DOM_SNAPSHOT_JS
from src.replay.escalation_seam import EscalationContext


class FakePage:
    def __init__(self, snapshots=None):
        self.init_scripts = []
        self.bindings = {}
        self.evaluate_calls = []            # (js, arg)
        self._snapshots = list(snapshots or [])
        self.snapshot_flag_log = []         # is_takeover_active captured at each snapshot evaluate
        self.handler_ref = None             # test sets this so the snapshot call can read the flag
        self._listeners = {}                # event -> [callbacks], for page.on/remove_listener

    def on(self, event, cb):
        self._listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        if cb in self._listeners.get(event, []):
            self._listeners[event].remove(cb)

    async def fire(self, event):
        """Simulate a Playwright page event (e.g. 'load' after a navigation) and let any tasks it schedules run."""
        for cb in list(self._listeners.get(event, [])):
            cb(self)
        await asyncio.sleep(0.02)

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def expose_binding(self, name, handler):
        self.bindings[name] = handler

    async def evaluate(self, js, arg=None):
        self.evaluate_calls.append((js, arg))
        if "IFAI_DOM_SNAPSHOT" in js:
            if self.handler_ref is not None:
                self.snapshot_flag_log.append(self.handler_ref.is_takeover_active)
            return self._snapshots.pop(0) if self._snapshots else {}
        return None

    def locator(self, selector):
        return MagicMock()


def _handler(page, tmp_path, cap="lookup_balance"):
    return PlaywrightEscalationHandler(page=page, evidence_dir=tmp_path, capability_name=cap)


def test_is_interactive_reflects_attended_flag(tmp_path):
    """Phase-3 regression guard: a headless/unattended handler MUST report is_interactive=False so discovery
    keeps its max_steps/stuck termination and replay maps stuck conditions to stub_unavailable instead of
    blocking forever on a panel nobody can click (the reactive escalate() has no timeout)."""
    prov = lambda: None  # noqa: E731
    assert PlaywrightEscalationHandler(page_provider=prov, evidence_dir=tmp_path).is_interactive is True
    assert PlaywrightEscalationHandler(page_provider=prov, evidence_dir=tmp_path,
                                       interactive=True).is_interactive is True
    assert PlaywrightEscalationHandler(page_provider=prov, evidence_dir=tmp_path,
                                       interactive=False).is_interactive is False


def _ctx(step="s1", reason="checkpoint_timeout"):
    return EscalationContext(step_id=step, reason=reason, current_url="http://app/x", observed_text="")


async def _escalate_with_click(handler, page, outcome, note="", snapshots=None):
    """Run escalate() as a task, then resolve it via the JS->Python binding (like a button click)."""
    if snapshots is not None:
        page._snapshots = list(snapshots)
    task = asyncio.create_task(handler.escalate(_ctx()))
    await asyncio.sleep(0.01)                           # let escalate() reach bridge.wait()
    await page.bindings["resumeAutomation"]({"page": page}, outcome, note)
    return await asyncio.wait_for(task, timeout=5)


async def _escalate_takeover(handler, page, note="", done_note="", snapshots=None):
    """Reactive take-over is now TWO-PHASE (Bug-1 fix): the 'takeover_resume' click switches the panel to
    planned mode, then a 'planned_done' click completes it and lets replay re-observe. Sends both clicks,
    guarded by wait_for so a wiring regression fails fast instead of hanging on the 5-min safety timeout."""
    if snapshots is not None:
        page._snapshots = list(snapshots)
    task = asyncio.create_task(handler.escalate(_ctx()))
    await asyncio.sleep(0.02)                           # reach the 1st (three-button) wait
    await page.bindings["resumeAutomation"]({"page": page}, "takeover_resume", note)
    await asyncio.sleep(0.02)                           # transition to planned mode + reach the 2nd wait
    await page.bindings["resumeAutomation"]({"page": page}, "planned_done", done_note)
    return await asyncio.wait_for(task, timeout=5)


# ---- contract 1: injection idempotent across navigation --------------------------------------------
async def test_contract1_injection_idempotent(tmp_path):
    page = FakePage()
    h = _handler(page, tmp_path)
    await h.install()
    await h.install()                                   # second call is a no-op
    assert len(page.init_scripts) == 1                  # add_init_script invoked exactly once
    assert list(page.bindings) == ["resumeAutomation"]
    # JS-side duplication guards (window flag + DOM id) — what keeps the panel single across navigations
    assert "if (window.__ifaiEscalation) return;" in PANEL_JS_SOURCE
    assert "if (document.getElementById(BAR_ID)) return;" in PANEL_JS_SOURCE


async def test_contract1_escalate_does_not_reinstall(tmp_path):
    page = FakePage(snapshots=[{}, {}])
    h = _handler(page, tmp_path)
    await _escalate_with_click(h, page, "resume")
    await _escalate_with_click(h, page, "resume")
    assert len(page.init_scripts) == 1                  # still one across two escalations


# ---- contract 2: collapsed bar renders (static assertion on the injected source) -------------------
def test_contract2_collapsed_bar_in_source():
    assert "interfaceai-escalation-bar" in PANEL_JS_SOURCE
    assert "width:40px" in PANEL_JS_SOURCE               # 40px right-edge bar
    assert "interfaceai-escalation-panel" in PANEL_JS_SOURCE
    assert "width:320px" in PANEL_JS_SOURCE              # 320px expanded panel


# ---- contract 3: escalate expands panel + paused state ---------------------------------------------
async def test_contract3_escalate_expands_and_pauses(tmp_path):
    page = FakePage(snapshots=[{}])
    h = _handler(page, tmp_path)
    await _escalate_with_click(h, page, "resume")
    # the panel-drive call is uniquely the arrow function "(ctx) => ..." (not the panel-install source)
    drive = [(js, arg) for (js, arg) in page.evaluate_calls if js.lstrip().startswith("(ctx) =>")]
    assert len(drive) == 1
    ctx_arg = drive[0][1]
    assert ctx_arg["capability"] == "lookup_balance" and ctx_arg["step"] == "s1"
    assert ctx_arg["reason"] == "checkpoint_timeout" and ctx_arg["url"] == "http://app/x"
    # the JS escalate() sets the paused (orange, pulsing) state
    assert "applyState('paused')" in PANEL_JS_SOURCE


# ---- contract 4: button click resolves the asyncio.Event -> outcome --------------------------------
@pytest.mark.parametrize("outcome", ["resume", "takeover_resume", "abort"])
async def test_contract4_button_resolves_event(tmp_path, outcome):
    page = FakePage(snapshots=[{}, {}])
    h = _handler(page, tmp_path)
    if outcome == "takeover_resume":
        out = await _escalate_takeover(h, page)        # two-phase: takeover_resume then planned Done
    else:
        out = await _escalate_with_click(h, page, outcome, note="")
    assert out.action == outcome


# ---- contract 5: operator note captured in the outcome ---------------------------------------------
async def test_contract5_operator_note_captured(tmp_path):
    page = FakePage(snapshots=[{}])
    h = _handler(page, tmp_path)
    out = await _escalate_with_click(h, page, "resume", note="entered the OTP from my phone")
    assert out.operator_note == "entered the OTP from my phone"


# ---- contract 6: DOM diff captured on takeover (structural, not verbatim) ---------------------------
async def test_contract6_dom_diff_on_takeover(tmp_path):
    page = FakePage(snapshots=[{"row": 10, "button": 2}, {"row": 13, "button": 1}])
    h = _handler(page, tmp_path)
    await _escalate_takeover(h, page)                  # before-snapshot at start, after-snapshot after Done
    ev = json.loads(next(Path(tmp_path).glob("escalation_*.json")).read_text(encoding="utf-8"))
    diff = ev["dom_diff_summary"]
    assert diff["added"] == 3 and diff["removed"] == 1 and diff["mutated"] == 0
    # privacy: only role names + integer deltas, never verbatim page text
    assert diff["by_role"] == {"row": 3, "button": -1}
    assert all(isinstance(k, str) and isinstance(v, int) for k, v in diff["by_role"].items())


def test_dom_diff_summary_structure():
    d = summarize({"row": 10, "button": 2}, {"row": 13})
    assert d == {"added": 3, "removed": 2, "mutated": 0, "by_role": {"row": 3, "button": -2}}


# ---- contract 7: is_takeover_active lifecycle ------------------------------------------------------
async def test_contract7_takeover_flag_lifecycle(tmp_path):
    page = FakePage(snapshots=[{"a": 1}, {"a": 2}])
    h = _handler(page, tmp_path)
    page.handler_ref = h
    assert h.is_takeover_active is False                # at rest
    await _escalate_takeover(h, page)
    assert h.is_takeover_active is False                # reset after DOM re-read completes
    # flag was False during the BEFORE snapshot and True during the AFTER (re-read, post-Done) snapshot
    assert page.snapshot_flag_log == [False, True]


async def test_contract7_flag_stays_false_for_resume(tmp_path):
    page = FakePage(snapshots=[{"a": 1}])
    h = _handler(page, tmp_path)
    page.handler_ref = h
    await _escalate_with_click(h, page, "resume")
    assert h.is_takeover_active is False
    assert page.snapshot_flag_log == [False]           # only the before-snapshot; no takeover re-read


# ---- Bug-1 fix: take-over switches to PLANNED mode and does NOT re-observe until Done -----------------
async def test_takeover_transitions_to_planned_mode_before_reobserving(tmp_path):
    page = FakePage(snapshots=[{"a": 1}, {"a": 2}])
    h = _handler(page, tmp_path)
    page.handler_ref = h
    task = asyncio.create_task(h.escalate(_ctx()))
    await asyncio.sleep(0.02)
    await page.bindings["resumeAutomation"]({"page": page}, "takeover_resume", "")
    await asyncio.sleep(0.02)
    # after the takeover_resume click the panel is in PLANNED mode and we are still WAITING — no re-observe yet
    assert not task.done()
    planned = [arg for (js, arg) in page.evaluate_calls
               if js.lstrip().startswith("(ctx) =>") and "escalatePlanned" in js]
    assert len(planned) == 1 and planned[0]["reason"] == "reactive_takeover_in_progress"
    assert page.snapshot_flag_log == [False]           # only the BEFORE snapshot; DOM NOT re-observed yet
    # human clicks Done -> re-observation happens exactly once, after Done
    await page.bindings["resumeAutomation"]({"page": page}, "planned_done", "did the thing")
    out = await asyncio.wait_for(task, timeout=5)
    assert out.action == "takeover_resume" and out.operator_note == "did the thing"
    assert page.snapshot_flag_log == [False, True]     # exactly one re-observe, and only after Done


async def test_takeover_replans_planned_mode_on_navigation(tmp_path):
    # Phase-B fix: navigating during take-over must NOT strand the human — planned mode (the Done button) is
    # re-applied on every page 'load' so it survives navigation.
    page = FakePage(snapshots=[{"a": 1}, {"a": 2}])
    h = _handler(page, tmp_path)
    task = asyncio.create_task(h.escalate(_ctx()))
    await asyncio.sleep(0.02)
    await page.bindings["resumeAutomation"]({"page": page}, "takeover_resume", "")
    await asyncio.sleep(0.02)
    assert page._listeners.get("load"), "no page 'load' listener registered during take-over wait"
    n_before = sum(1 for (js, _a) in page.evaluate_calls if "escalatePlanned" in js)
    await page.fire("load")                              # the human navigated
    n_after = sum(1 for (js, _a) in page.evaluate_calls if "escalatePlanned" in js)
    assert n_after == n_before + 1                       # planned mode re-applied on the new page
    await page.bindings["resumeAutomation"]({"page": page}, "planned_done", "done after nav")
    out = await asyncio.wait_for(task, timeout=5)
    assert out.action == "takeover_resume" and out.operator_note == "done after nav"
    assert not page._listeners.get("load")               # listener cleaned up after the wait


async def test_takeover_timeout_returns_exhausted(tmp_path, monkeypatch):
    import src.escalation.handler as H
    monkeypatch.setattr(H, "_REACTIVE_TIMEOUT_S", 0.3)   # shrink the safety timeout for the test
    page = FakePage(snapshots=[{"a": 1}, {"a": 2}])
    h = _handler(page, tmp_path)
    task = asyncio.create_task(h.escalate(_ctx()))
    await asyncio.sleep(0.02)
    await page.bindings["resumeAutomation"]({"page": page}, "takeover_resume", "")
    # never click Done -> the planned-mode wait times out -> escalation exhausted (never hangs)
    out = await asyncio.wait_for(task, timeout=5)
    assert out.action == "exhausted"
    ev = json.loads(next(Path(tmp_path).glob("escalation_*.json")).read_text(encoding="utf-8"))
    assert ev["human_outcome"] == "timeout"


# ---- contract 8: evidence event written with the §9 schema -----------------------------------------
async def test_contract8_evidence_event_written(tmp_path):
    page = FakePage(snapshots=[{}])
    h = _handler(page, tmp_path)
    await _escalate_with_click(h, page, "resume", note="looked fine")
    files = list(Path(tmp_path).glob("escalation_*.json"))
    assert len(files) == 1
    ev = json.loads(files[0].read_text(encoding="utf-8"))
    assert set(ev) == {"escalation_at_step", "reason", "timestamp", "human_outcome",
                       "duration_s", "dom_diff_summary", "operator_note"}
    assert ev["escalation_at_step"] == "s1" and ev["reason"] == "checkpoint_timeout"
    assert ev["human_outcome"] == "resume" and ev["operator_note"] == "looked fine"
    assert isinstance(ev["duration_s"], float) and ev["dom_diff_summary"] is None
    assert isinstance(ev["timestamp"], str)


# ---- additional: multiple sequential escalations reset state ---------------------------------------
async def test_sequential_escalations(tmp_path):
    page = FakePage(snapshots=[{}, {}])
    h = _handler(page, tmp_path)
    for i, oc in enumerate(["resume", "abort"]):
        task = asyncio.create_task(h.escalate(_ctx(step=f"s{i}")))
        await asyncio.sleep(0.01)
        await page.bindings["resumeAutomation"]({}, oc, f"note{i}")
        out = await task
        assert out.action == oc
    assert len(page.init_scripts) == 1                              # installed once across both
    assert len(list(Path(tmp_path).glob("escalation_*.json"))) == 2  # one event per escalation


# ---- additional: reactive escalation blocks until resolved (within the safety timeout) --------------
async def test_blocks_until_resolved(tmp_path):
    page = FakePage(snapshots=[{}])
    h = _handler(page, tmp_path)
    task = asyncio.create_task(h.escalate(_ctx()))
    await asyncio.sleep(0.05)
    assert not task.done()                              # still blocked — waiting for a human (bounded by 5 min)
    await page.bindings["resumeAutomation"]({}, "resume", "")
    assert (await task).action == "resume"


# ---- additional: binding rejects an invalid outcome (JS->Python safety) ----------------------------
async def test_binding_rejects_invalid_outcome():
    bridge = ResumeBridge()
    res = await bridge.on_resume({}, "delete_everything")
    assert res["ok"] is False and "invalid" in res["error"]
    assert not bridge._event.is_set()                  # a bogus outcome never resolves the wait


# ---- additional: PANEL_JS_SOURCE validity ----------------------------------------------------------
def test_panel_source_validity():
    assert isinstance(PANEL_JS_SOURCE, str) and len(PANEL_JS_SOURCE) > 500
    for token in ("__ifaiEscalation", "resumeAutomation", "escalate", "setState",
                  "interfaceai-escalation-bar", "interfaceai-escalation-panel",
                  "resume", "takeover_resume", "abort", "@keyframes ifai-pulse"):
        assert token in PANEL_JS_SOURCE, token
    assert PANEL_JS_SOURCE.count("(") == PANEL_JS_SOURCE.count(")")


def test_dom_snapshot_js_is_structural_only():
    # the snapshot JS must count elements, never read their text -> no textContent/innerText/value reads
    assert "querySelectorAll('*')" in DOM_SNAPSHOT_JS
    for banned in ("textContent", "innerText", ".value"):
        assert banned not in DOM_SNAPSHOT_JS
