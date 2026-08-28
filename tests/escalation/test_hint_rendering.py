"""Unit tests for escalation-panel hint rendering (D3-α; ADR-007 revision).

Mock-based (FakePage records the ctx dict driven into the panel; the JS itself is asserted structurally on
PANEL_JS_SOURCE). Covers contract items 5 (hint present -> section; None -> absent) and 6 (reactive, planned,
and takeover->planned modes all carry the hint).
"""
from __future__ import annotations

import asyncio

import pytest

from src.escalation import PANEL_JS_SOURCE, PlaywrightEscalationHandler
from src.replay.escalation_seam import EscalationContext


class FakePage:
    def __init__(self, snapshots=None):
        self.init_scripts = []
        self.bindings = {}
        self.evaluate_calls = []              # (js, arg)
        self._snapshots = list(snapshots or [{}, {}])
        self._listeners = {}

    def on(self, event, cb):
        self._listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        if cb in self._listeners.get(event, []):
            self._listeners[event].remove(cb)

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def expose_binding(self, name, handler):
        self.bindings[name] = handler

    async def evaluate(self, js, arg=None):
        self.evaluate_calls.append((js, arg))
        if "IFAI_DOM_SNAPSHOT" in js:
            return self._snapshots.pop(0) if self._snapshots else {}
        return None


def _handler(page, tmp_path):
    return PlaywrightEscalationHandler(page=page, evidence_dir=tmp_path, capability_name="lookup_balance")


def _ctx(hint):
    return EscalationContext(step_id="step_03", reason="checkpoint_timeout", current_url="http://app/x",
                             observed_text="", hint=hint)


def _reactive_ctx_args(page):
    return [arg for (js, arg) in page.evaluate_calls if js.lstrip().startswith("(ctx) =>")
            and "escalate(ctx)" in js]


def _planned_ctx_args(page):
    return [arg for (js, arg) in page.evaluate_calls if js.lstrip().startswith("(ctx) =>")
            and "escalatePlanned(ctx)" in js]


# ---------------- item 5/6: reactive mode carries the hint (and None) ----------------

async def test_reactive_escalate_passes_hint(tmp_path):
    page = FakePage()
    h = _handler(page, tmp_path)
    task = asyncio.create_task(h.escalate(_ctx("Open the account activity page to read its balance")))
    await asyncio.sleep(0.01)
    await page.bindings["resumeAutomation"]({"page": page}, "resume", "")
    await asyncio.wait_for(task, timeout=5)
    args = _reactive_ctx_args(page)
    assert args and args[0]["hint"] == "Open the account activity page to read its balance"


async def test_reactive_escalate_hint_none(tmp_path):
    page = FakePage()
    h = _handler(page, tmp_path)
    task = asyncio.create_task(h.escalate(_ctx(None)))
    await asyncio.sleep(0.01)
    await page.bindings["resumeAutomation"]({"page": page}, "resume", "")
    await asyncio.wait_for(task, timeout=5)
    args = _reactive_ctx_args(page)
    assert args and args[0]["hint"] is None                    # panel hides the row when hint is None


# ---------------- item 6: planned mode carries the hint ----------------

async def test_planned_escalate_passes_hint(tmp_path):
    page = FakePage()
    h = _handler(page, tmp_path)
    task = asyncio.create_task(h.escalate_planned("Enter the 2FA code", "2fa", timeout_ms=5000,
                                                  hint="Provide the one-time code from your device"))
    await asyncio.sleep(0.01)
    await page.bindings["resumeAutomation"]({"page": page}, "planned_done", "")
    await asyncio.wait_for(task, timeout=5)
    args = _planned_ctx_args(page)
    assert args and args[0]["hint"] == "Provide the one-time code from your device"
    assert args[0]["prompt"].startswith("Enter the 2FA code")   # Gap B appends action guidance


# ---------------- item 6: takeover -> planned mode carries the hint ----------------

async def test_takeover_then_planned_carries_hint(tmp_path):
    page = FakePage(snapshots=[{}, {}])
    h = _handler(page, tmp_path)
    task = asyncio.create_task(h.escalate(_ctx("Read the checking balance from the activity table")))
    await asyncio.sleep(0.02)
    await page.bindings["resumeAutomation"]({"page": page}, "takeover_resume", "")   # -> switches to planned
    await asyncio.sleep(0.02)
    await page.bindings["resumeAutomation"]({"page": page}, "planned_done", "fixed it")
    await asyncio.wait_for(task, timeout=5)
    # the reactive ctx AND the takeover planned-mode ctx both carry the hint (item 6: all 3 modes)
    assert _reactive_ctx_args(page)[0]["hint"] == "Read the checking balance from the activity table"
    assert _planned_ctx_args(page)[0]["hint"] == "Read the checking balance from the activity table"


# ---------------- item 5: the panel JS renders/hides the "About this step:" row ----------------

def test_panel_source_has_hint_row_and_render_logic():
    assert "ifai-hint" in PANEL_JS_SOURCE                       # the row element exists
    assert "About this step: " in PANEL_JS_SOURCE               # D3-α label
    assert "function setHint" in PANEL_JS_SOURCE
    # both modes call setHint with the ctx hint
    assert PANEL_JS_SOURCE.count("setHint(ctx.hint)") == 2
    # empty/null hint hides the whole row (no dangling label)
    assert "e.style.display = 'none'" in PANEL_JS_SOURCE
    assert 'style="display:none"' in PANEL_JS_SOURCE            # row starts hidden
