"""PlaywrightExecutor — the single public entry point (ADR-002 backend seam; ADR-007/ADR-8 headed default).

Holds one browser + context + page. Translates artifact Locators/Actions/Checkpoints into Playwright
calls. Strictly no LLM, no artifact I/O, no cross-step orchestration, no takeover overlay (those belong to
agent/replay/storage/escalation).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright

from . import action_dispatcher
from .checkpoint_resolver import resolve_checkpoint as _resolve_checkpoint
from .evidence import EvidenceCapture
from .locator_resolver import build_pw_locator as _build_pw_locator
from .locator_resolver import resolve_locator as _resolve_locator
from .results import ActionResult, CheckpointResult, VariableScope

_log = logging.getLogger("executor")


class PlaywrightExecutor:
    def __init__(self, evidence_dir: Path, base_url: Optional[str] = None, headless: bool = False,
                 safety_gate=None) -> None:
        # headless defaults to False (ADR-007/ADR-8: headed is the takeover-capable mode). Tests pass True.
        self.evidence_dir = Path(evidence_dir)
        self.base_url = base_url
        self.headless = headless
        # Duck-typed safety gate (ADR-008 §Safety). None = off (default, discovery/tests); the CLI replay path
        # injects an enforcing SafetyGate. Checked in execute_action before every dispatch.
        self.safety_gate = safety_gate
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self.evidence: Optional[EvidenceCapture] = None
        # Injected (duck-typed) escalation handler for planned human-input steps (ADR-007). Set by the
        # ReplayEngine at replay start; None during discovery (the agent handles the tool directly). Kept as
        # an injected attribute so the executor never imports src/escalation (ADR-008 boundary).
        self.escalation_handler = None

    def get_current_page(self):
        """Accessor for the current Playwright page (ADR-9 Blocker-1: escalation handler's page_provider uses
        this instead of reaching for a raw `.page`)."""
        return self.page

    def set_mask_locators(self, model_locators, scope: VariableScope) -> None:
        """Register the elements whose regions must be masked in every evidence screenshot (§3.4). The
        ReplayEngine passes the locators of `sensitive: true`-bound steps; we build lazy Playwright locators
        (best-effort — an unbuildable one is skipped, and a locator matching nothing at screenshot time is
        simply ignored by Playwright's mask). No-op if the executor is not started."""
        if self.page is None or self.evidence is None:
            return
        built = []
        for loc in model_locators or []:
            try:
                built.append(_build_pw_locator(self.page, loc, scope))
            except Exception as exc:  # noqa: BLE001 - a locator we can't build just isn't masked
                _log.debug("mask locator skipped (unbuildable): %s", exc)
        self.evidence.mask_locators = built

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        ctx_kwargs = {"base_url": self.base_url} if self.base_url else {}
        self._context = await self._browser.new_context(**ctx_kwargs)
        self.page = await self._context.new_page()
        # Gap 2 smart-dismiss (ADR-007 revision): registering a dialog listener DISABLES Playwright's default
        # auto-dismiss, so WE choose per type. Dismiss alert + beforeunload (non-blocking side effects we don't
        # want to freeze on; beforeunload=dismiss is the user-locked choice for this deliverable — the design
        # review's alternative was accept). Let confirm + prompt FALL THROUGH unhandled — the page then blocks,
        # the checkpoint times out, and reactive escalation hands control to the operator. (NOTE: true HTTP
        # Basic Auth is NOT a JS `dialog` event — it needs http_credentials on the context, separate future
        # work, not this handler; see docs/hints_and_dialogs_review.md note 5a.)
        self.page.on("dialog", self._handle_dialog)
        self.evidence = EvidenceCapture(self.page, self.evidence_dir)
        _log.info("executor started (headless=%s, base_url=%s)", self.headless, self.base_url)

    async def _handle_dialog(self, dialog) -> None:
        """Smart-dismiss (Gap 2 Level A refined). alert/beforeunload -> dismiss; confirm/prompt -> leave
        unresolved on purpose (page hangs -> checkpoint timeout -> reactive escalation)."""
        if dialog.type in ("alert", "beforeunload"):
            _log.info("[dialog] auto-dismissing %s: %r", dialog.type, dialog.message)
            await dialog.dismiss()
        else:
            _log.info("[dialog] letting %s dialog fall through (message=%r) -> checkpoint timeout will "
                      "escalate", dialog.type, dialog.message)

    async def stop(self) -> None:
        # Clean shutdown; guard each resource so one failure doesn't leak the others.
        for name, closer in (
            ("context", getattr(self._context, "close", None)),
            ("browser", getattr(self._browser, "close", None)),
            ("playwright", getattr(self._pw, "stop", None)),
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - best-effort teardown
                _log.warning("error closing %s: %s", name, exc)
        self._context = self._browser = self._pw = self.page = self.evidence = None
        _log.info("executor stopped")

    async def execute_action(self, action, scope: VariableScope) -> ActionResult:
        self._require_started()
        # Safety pre-dispatch (ADR-008 §Safety): allowlist + action-type gate BEFORE any Playwright action. A
        # violation raises SafetyViolationError, which propagates to the replay engine and becomes
        # hard_failure(reason="safety_blocked:<rule>"). safety_gate is None (off) unless injected by the CLI.
        if self.safety_gate is not None:
            self.safety_gate.check_action(action, scope)
        return await action_dispatcher.dispatch(self, action, scope)

    async def resolve_checkpoint(self, checkpoint, scope: VariableScope,
                                 capture_evidence: bool = True,
                                 business_only: bool = False) -> CheckpointResult:
        self._require_started()
        return await _resolve_checkpoint(self.page, checkpoint, scope, self.evidence,
                                         capture_evidence=capture_evidence, business_only=business_only)

    async def resolve_locator(self, locator, scope: VariableScope):
        """Public because src/replay/ may want to resolve a locator without executing an action.
        Returns a Playwright locator matching exactly one element (or raises)."""
        self._require_started()
        return await _resolve_locator(self.page, locator, scope, self.evidence)

    def _require_started(self) -> None:
        if self.page is None:
            raise RuntimeError("PlaywrightExecutor not started; call `await start()` first")
