"""Gap A (panel state persists across navigation) + Gap B (prompt action-guidance) unit tests. Mocked."""
from __future__ import annotations

import asyncio

import pytest

from src.escalation.handler import PlaywrightEscalationHandler, format_human_prompt


# ---------------- Gap B: format_human_prompt ----------------

def test_format_credential_prompt_says_do_not_submit():
    out = format_human_prompt("Please enter the username and password for ParaBank")
    assert "do NOT click" in out.lower() or "do not click" in out.lower()
    assert "Done" in out and out.startswith("Please enter the username")


def test_format_generic_prompt_has_done_tail():
    out = format_human_prompt("Solve the captcha challenge")
    assert out.startswith("Solve the captcha challenge")
    assert "click Done here to continue" in out
    assert "do NOT click" not in out          # generic prompt is not a credential prompt


def test_format_is_idempotent():
    once = format_human_prompt("enter password")
    assert format_human_prompt(once) == once  # arrow-mark guard prevents double-append


def test_format_handles_empty():
    assert "Done" in format_human_prompt("")
    assert "Done" in format_human_prompt(None)


# ---------------- Gap A: panel re-injected on navigation while awaiting the human ----------------

class NavFakePage:
    """Records driven ctx + 'load' listeners so a test can simulate a navigation mid-escalation."""

    def __init__(self):
        self.bindings = {}
        self.planned_drives = []          # prompts driven via escalatePlanned
        self._listeners = {}

    def on(self, event, cb):
        self._listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        if cb in self._listeners.get(event, []):
            self._listeners[event].remove(cb)

    def fire(self, event):
        for cb in list(self._listeners.get(event, [])):
            cb()

    async def add_init_script(self, s):
        ...

    async def expose_binding(self, name, handler):
        self.bindings[name] = handler

    async def evaluate(self, js, arg=None):
        # record only the arrow-fn DRIVE calls, not the panel-install source (which also defines escalatePlanned)
        if isinstance(js, str) and js.lstrip().startswith("(ctx) =>") and "escalatePlanned(ctx)" in js and arg:
            self.planned_drives.append(arg["prompt"])
        return {}


async def test_planned_panel_reinjected_on_navigation(tmp_path):
    page = NavFakePage()
    h = PlaywrightEscalationHandler(page=page, evidence_dir=tmp_path, capability_name="cap")
    task = asyncio.create_task(h.escalate_planned("Enter the username", "credentials", timeout_ms=5000))
    await asyncio.sleep(0.01)
    assert len(page.planned_drives) == 1                     # driven once initially
    page.fire("load")                                        # simulate a navigation (user clicked LOG IN)
    await asyncio.sleep(0.01)
    assert len(page.planned_drives) == 2                     # re-injected on the new page (Gap A)
    assert page.planned_drives[1] == page.planned_drives[0]  # same (wrapped) prompt restored
    await page.bindings["resumeAutomation"]({}, "planned_done", "")   # human finally clicks Done
    await asyncio.wait_for(task, timeout=5)
    assert page._listeners.get("load") == []                 # listener cleaned up after the wait
