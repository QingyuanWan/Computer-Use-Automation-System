"""Phase-2 planned-mode escalation tests (ADR-007) — contract items 6, 7. All mocked, no browser."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.escalation import PANEL_JS_SOURCE, PlaywrightEscalationHandler


class FakePage:
    def __init__(self, snapshots=None):
        self.init_scripts = []
        self.bindings = {}
        self.evaluate_calls = []
        self._snapshots = list(snapshots or [])
        self.snapshot_flag_log = []
        self.handler_ref = None

    def on(self, event, cb):                 # Gap A: _wait_persisting registers a page 'load' listener
        pass

    def remove_listener(self, event, cb):
        pass

    async def add_init_script(self, s):
        self.init_scripts.append(s)

    async def expose_binding(self, name, handler):
        self.bindings[name] = handler

    async def evaluate(self, js, arg=None):
        self.evaluate_calls.append((js, arg))
        if "IFAI_DOM_SNAPSHOT" in js:
            if self.handler_ref is not None:
                self.snapshot_flag_log.append(self.handler_ref.is_takeover_active)
            return self._snapshots.pop(0) if self._snapshots else {}
        return None

    def locator(self, s):
        return MagicMock()


def _h(page, tmp_path):
    return PlaywrightEscalationHandler(page=page, evidence_dir=tmp_path, capability_name="cap")


# ---- panel planned mode content (contract 7) -------------------------------------------------------
def test_panel_has_planned_mode():
    for tok in ("escalatePlanned", "planned_done", "ifai-prompt", "Done", "'planned'", "ifai-planned"):
        assert tok in PANEL_JS_SOURCE, tok


# ---- escalate_planned happy path: Done resolves, void return, evidence planned_done -----------------
async def test_escalate_planned_done(tmp_path):
    page = FakePage(snapshots=[{}, {}])
    h = _h(page, tmp_path)
    task = asyncio.create_task(h.escalate_planned("Enter the 2FA code", "2fa", timeout_ms=60000))
    await asyncio.sleep(0.01)
    # the panel was driven into planned mode (the arrow-fn drive call, not the panel-install source)
    drives = [arg for js, arg in page.evaluate_calls if isinstance(js, str) and js.lstrip().startswith("(ctx) =>")]
    assert len(drives) == 1
    assert drives[0]["prompt"].startswith("Enter the 2FA code") and drives[0]["reason"] == "2fa"
    await page.bindings["resumeAutomation"]({}, "planned_done", "did 2fa")   # human clicks Done
    result = await task
    assert result is None                                  # void return
    ev = json.loads(next(Path(tmp_path).glob("escalation_*.json")).read_text(encoding="utf-8"))
    assert ev["human_outcome"] == "planned_done" and ev["operator_note"] == "did 2fa"


# ---- timeout → raises asyncio.TimeoutError + records a 'timeout' event ------------------------------
async def test_escalate_planned_timeout(tmp_path):
    page = FakePage(snapshots=[{}])
    h = _h(page, tmp_path)
    with pytest.raises(asyncio.TimeoutError):
        await h.escalate_planned("prompt", "2fa", timeout_ms=20)   # 20ms, nobody clicks Done
    ev = json.loads(next(Path(tmp_path).glob("escalation_*.json")).read_text(encoding="utf-8"))
    assert ev["human_outcome"] == "timeout"
    assert h.is_takeover_active is False                            # never stuck True


# ---- is_takeover_active toggles during the planned DOM re-read (contract 7) -------------------------
async def test_takeover_flag_during_planned(tmp_path):
    page = FakePage(snapshots=[{"a": 1}, {"a": 2}])
    h = _h(page, tmp_path)
    page.handler_ref = h
    assert h.is_takeover_active is False
    task = asyncio.create_task(h.escalate_planned("p", "r"))
    await asyncio.sleep(0.01)
    await page.bindings["resumeAutomation"]({}, "planned_done", "")
    await task
    assert h.is_takeover_active is False
    assert page.snapshot_flag_log == [False, True]                 # before=False, after(re-read)=True
