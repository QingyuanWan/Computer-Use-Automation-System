"""Unit tests for the smart-dismiss dialog handler (Gap 2 Level A refined; ADR-007 revision).

No real browser: `PlaywrightExecutor._handle_dialog` is called directly with a fake Dialog. Contract items
7 (dismiss alert + beforeunload; confirm + prompt fall through) and 8 (fall-through leaves the dialog for
the checkpoint-timeout → reactive-escalation path).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.executor import PlaywrightExecutor


class _FakeDialog:
    def __init__(self, type_):
        self.type = type_
        self.message = f"<{type_} message>"
        self.dismiss = AsyncMock()
        self.accept = AsyncMock()


def _executor(tmp_path):
    # __init__ only sets attributes; no browser is started, so _handle_dialog can be exercised in isolation.
    return PlaywrightExecutor(evidence_dir=tmp_path / "ev", headless=True)


@pytest.mark.parametrize("dtype", ["alert", "beforeunload"])
async def test_dismisses_alert_and_beforeunload(tmp_path, dtype):
    ex = _executor(tmp_path)
    d = _FakeDialog(dtype)
    await ex._handle_dialog(d)
    d.dismiss.assert_awaited_once()
    d.accept.assert_not_called()


@pytest.mark.parametrize("dtype", ["confirm", "prompt"])
async def test_confirm_and_prompt_fall_through(tmp_path, dtype):
    ex = _executor(tmp_path)
    d = _FakeDialog(dtype)
    await ex._handle_dialog(d)
    # item 8: the handler does NOT resolve confirm/prompt — the dialog stays open so the page hangs, the
    # checkpoint times out, and reactive escalation engages (giving the operator explicit control).
    d.dismiss.assert_not_called()
    d.accept.assert_not_called()
