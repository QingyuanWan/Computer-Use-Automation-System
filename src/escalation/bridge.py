"""JS -> Python resume bridge (ADR-007). The overlay's buttons call `window.resumeAutomation(outcome, note)`,
registered via page.expose_binding; that routes here, validates the outcome, and releases the asyncio.Event
the paused escalate() coroutine awaits."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

_VALID_OUTCOMES = ("resume", "takeover_resume", "abort", "planned_done")

# Wake the event loop this often while blocked on a human. Two reasons (Phase-3 bug fix): (1) it bounds the
# wait so we never block forever, and (2) on Windows the Proactor loop only delivers a pending SIGINT when it
# wakes — a single long `await event.wait()` swallows Ctrl+C, so we slice the wait to keep it interruptible.
_POLL_SLICE_S = 0.5


class ResumeBridge:
    """One bridge per handler; `reset()` before each escalation so a stale click can't resolve the next one."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._payload: Optional[dict[str, Any]] = None

    def reset(self) -> None:
        self._event = asyncio.Event()
        self._payload = None

    async def on_resume(self, source: Any, outcome: str, note: str = "") -> dict[str, Any]:
        """expose_binding callback: Playwright passes (source, *js_args). Rejects any outcome that is not one
        of the three sanctioned values — a compromised/injected page cannot drive an arbitrary action."""
        if outcome not in _VALID_OUTCOMES:
            return {"ok": False, "error": f"invalid outcome {outcome!r}"}
        self._payload = {"outcome": outcome, "note": (note or None)}
        self._event.set()
        return {"ok": True}

    async def wait(self, timeout: float) -> dict[str, Any]:
        """Block until a valid button press arrives, or raise asyncio.TimeoutError after `timeout` seconds.
        Polls in short slices (`_POLL_SLICE_S`) so the wait is bounded AND stays Ctrl+C-interruptible on the
        Windows Proactor loop (a single long await would swallow SIGINT). `timeout` is mandatory — no caller
        may block indefinitely (Phase-3 bug fix; Phase A's untimed wait is gone)."""
        deadline = time.monotonic() + timeout
        while not self._event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=min(_POLL_SLICE_S, remaining))
            except asyncio.TimeoutError:
                continue   # slice elapsed with no press; re-check (this wake-up is what lets Ctrl+C surface)
        assert self._payload is not None
        return self._payload
