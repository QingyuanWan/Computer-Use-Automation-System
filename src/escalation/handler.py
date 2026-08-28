"""PlaywrightEscalationHandler (ADR-007) — the real overlay-injecting EscalationHandler that replaces
StubEscalationHandler. Constructor-injected into ReplayEngine exactly like the stub (no engine change).

Flow of escalate(): install the overlay once (add_init_script + expose_binding) -> snapshot the DOM ->
drive the panel to 'paused'/expanded -> await the operator's button via the ResumeBridge -> on
'takeover_resume', mark takeover active, RE-READ the DOM (ADR-7 "re-read before the next step"), diff it,
clear takeover -> write the §9 evidence event -> return the EscalationOutcome.

Boundary (ADR-008): imports only src/replay/escalation_seam (the ABC) + local escalation modules + stdlib.
NOTE (deviation): EscalationContext carries no capability_name, so it is a constructor arg here; the
CLI/caller sets it. is_takeover_active is a readable property; the agent recorder will consult it to skip
writing ARIA/screenshots during takeover (wiring deferred — Phase A only exposes the flag).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable

from src.replay.escalation_seam import EscalationContext, EscalationHandler, EscalationOutcome

from .bridge import ResumeBridge
from .dom_diff import DOM_SNAPSHOT_JS, summarize
from .evidence_writer import write_escalation_event
from .panel_script import PANEL_JS_SOURCE

_DRIVE_ESCALATE = "(ctx) => window.__ifaiEscalation && window.__ifaiEscalation.escalate(ctx)"
_DRIVE_PLANNED = "(ctx) => window.__ifaiEscalation && window.__ifaiEscalation.escalatePlanned(ctx)"
_DRIVE_STATE = "(s) => window.__ifaiEscalation && window.__ifaiEscalation.setState(s)"

# Hard safety timeout for REACTIVE escalation, which carries no per-step timeout the way a planned
# human_input step does. A human sees the panel and acts; if nobody responds within this window we treat the
# escalation as exhausted rather than block forever (Phase-3 bug fix).
_REACTIVE_TIMEOUT_S = 300   # 5 minutes

# After "Take over & resume", the panel switches to PLANNED mode so the human actually has time to act in the
# browser; replay only re-observes the DOM once the human clicks Done (Bug-1 fix, user-approved Option 2).
_TAKEOVER_PROMPT = ("You are now in control. Perform any actions needed in the browser (navigate, click, fill "
                    "fields), then click Done to resume replay.")
_TAKEOVER_REASON = "reactive_takeover_in_progress"

# Gap B: words that mark a prompt as a CREDENTIAL request → the human must TYPE but not SUBMIT (the automation
# clicks Log In itself; a natural "fill + click Log In" reflex navigates away and strands the intervention).
_CREDENTIAL_WORDS = ("username", "user name", "user id", "password", "credential", "login", "log in",
                     "sign in", "email", "pin", "otp", "passcode", "2fa", "one-time")
_CRED_GUIDANCE = ("\n\n➡ TYPE the requested value(s) into the browser's form fields, but do NOT click "
                  "Log In / Submit — the automation will do that for you. When you have finished typing, "
                  "click Done here.")
_GENERIC_GUIDANCE = ("\n\n➡ When you have completed the requested action in the browser, click Done here "
                     "to continue.")


def format_human_prompt(prompt: "str | None") -> str:
    """Append explicit action guidance to a planned-intervention prompt (Gap B) so a natural evaluator reflex
    (fill a form AND submit it) does not break the flow. Credential prompts get 'type but do NOT submit'; all
    others get a generic 'click Done when finished'. Idempotent — never appends twice (checks the arrow mark)."""
    p = (prompt or "Human input needed.").strip()
    if "➡" in p:                       # already carries guidance -> leave as-is
        return p
    low = p.lower()
    return p + (_CRED_GUIDANCE if any(w in low for w in _CREDENTIAL_WORDS) else _GENERIC_GUIDANCE)


class PlaywrightEscalationHandler(EscalationHandler):
    def __init__(self, page_provider: Callable[[], Any] = None, *, page: Any = None,
                 evidence_dir, capability_name: str = "unknown", interactive: bool = True) -> None:
        """`page_provider` is a lazy `Callable[[], Page|None]` (ADR-9 Blocker-1: no raw `.page` leaked; the
        CLI passes `executor.get_current_page`). `page=` is accepted as a convenience (wrapped in a provider)
        so a caller/test with a concrete page still works.

        `interactive` (Phase-3): whether a human can ACTUALLY see and click the overlay right now — i.e. the
        browser is headed and attended. Discovery/replay use `is_interactive` to decide whether a stuck
        condition escalates to a human vs. maps straight to `hard_failure(stub_unavailable)` (D6 "no human
        reachable"). The CLI is always headed (ADR-7/8) so it passes `interactive=True`; an unattended caller
        (e.g. the validation gate, which uses its non-interactive stub default) reports False. Regardless of
        this flag, every escalation wait is now bounded (see bridge.wait / _REACTIVE_TIMEOUT_S), so no run can
        block forever even if a panel is shown but never answered."""
        if page_provider is None and page is not None:
            page_provider = lambda: page   # noqa: E731 - trivial wrapper
        if page_provider is None:
            raise ValueError("PlaywrightEscalationHandler needs a page_provider (or page)")
        self._page_provider = page_provider
        self.evidence_dir = Path(evidence_dir)
        self.capability_name = capability_name
        self.is_interactive = interactive    # instance attr shadows the ABC's class default (True)
        self._bridge = ResumeBridge()
        self._takeover_active = False
        self._installed = False

    def _page(self) -> Any:
        return self._page_provider()

    @property
    def is_takeover_active(self) -> bool:
        """False at rest; True only while a takeover / planned intervention's DOM re-read is in progress
        (the recorder suspends observation while this is True)."""
        return self._takeover_active

    async def install(self) -> bool:
        """Idempotent: register the JS->Python binding + the persistent init script once, and inject into the
        CURRENT document. Returns False (no-op) if there is currently no page."""
        if self._installed:
            return True
        page = self._page()
        if page is None:
            return False
        await page.expose_binding("resumeAutomation", self._bridge.on_resume)
        await page.add_init_script(PANEL_JS_SOURCE)   # runs on every future navigation (survives nav)
        await page.evaluate(PANEL_JS_SOURCE)          # build it on the already-loaded page too
        self._installed = True
        return True

    async def _dom_snapshot(self) -> dict[str, int]:
        page = self._page()
        if page is None:
            return {}
        try:
            snap = await page.evaluate(DOM_SNAPSHOT_JS)
        except Exception:  # noqa: BLE001 - a snapshot failure must not break the escalation
            return {}
        return snap if isinstance(snap, dict) else {}

    async def _drive(self, expr: str, arg: Any = None) -> None:
        page = self._page()
        if page is not None:
            await page.evaluate(expr, arg) if arg is not None else await page.evaluate(expr)

    async def _wait_persisting(self, drive_expr: str, ctx: dict, timeout_s: float) -> dict:
        """Gap-A fix: await the human's button press while keeping the panel visible on whatever page is
        currently rendered. `add_init_script` rebuilds the panel in its DEFAULT (collapsed/reactive/running)
        state on every new document, so a navigation during the wait (e.g. the human types credentials and
        clicks LOG IN, navigating to overview.htm) would strand the in-flight escalation — the planned Done
        button would vanish and the wait would time out. On every page `load` we re-drive `drive_expr`+`ctx`
        to restore the in-flight state. Invariant: as long as we are awaiting the human, the panel is present
        and in the correct mode on the current page. Raises asyncio.TimeoutError on timeout."""
        page = self._page()

        async def _replan() -> None:
            try:
                await self._drive(drive_expr, ctx)     # restore the in-flight escalation after a navigation
            except Exception:  # noqa: BLE001 - a redraw failure must never break the wait/teardown
                pass

        def _on_load(*_a) -> None:
            asyncio.create_task(_replan())

        if page is not None:
            page.on("load", _on_load)
        try:
            return await self._bridge.wait(timeout_s)
        finally:
            if page is not None:
                try:
                    page.remove_listener("load", _on_load)
                except Exception:  # noqa: BLE001 - listener may already be gone
                    pass

    async def escalate(self, context: EscalationContext) -> EscalationOutcome:
        if not await self.install():                   # no page -> cannot show a panel; fail safe to abort
            return EscalationOutcome(action="abort", operator_note=None)
        self._bridge.reset()
        t0 = time.time()

        before = await self._dom_snapshot()
        reactive_ctx = {
            "capability": self.capability_name, "step": context.step_id,
            "reason": context.reason, "url": context.current_url, "hint": context.hint}
        await self._drive(_DRIVE_ESCALATE, reactive_ctx)
        # exactly ONE terminal info line per escalation event (the full detail is in the in-browser panel)
        print(f"[escalation] paused at step '{context.step_id}' (reason={context.reason}) — respond in the "
              f"in-browser panel (Resume / Take over / Abort). Auto-aborts after {_REACTIVE_TIMEOUT_S}s.",
              flush=True)

        outcome = "exhausted"      # EscalationOutcome.action returned to the engine (default if we time out)
        human_outcome = "exhausted"  # label recorded in the §9 evidence event
        note = None
        dom_diff = None
        try:
            try:
                payload = await self._wait_persisting(_DRIVE_ESCALATE, reactive_ctx, _REACTIVE_TIMEOUT_S)
            except asyncio.TimeoutError:               # nobody responded -> escalation exhausted (never hang)
                note = f"no response within {_REACTIVE_TIMEOUT_S}s; escalation exhausted"
                await self._drive(_DRIVE_STATE, "blocked")
                return EscalationOutcome(action="exhausted", operator_note=note)   # finally still writes the event
            action = payload["outcome"]
            note = payload.get("note")
            if action == "takeover_resume":
                # Bug-1 fix: DON'T dismiss + re-observe now. Hand control to the human via PLANNED mode and wait
                # for Done, so they actually have time to act in the browser before we re-check the checkpoint.
                takeover = await self._await_takeover_done(context)
                if takeover is None:                   # the human never clicked Done -> exhausted
                    human_outcome = "timeout"
                    note = f"take-over not completed within {_REACTIVE_TIMEOUT_S}s"
                    return EscalationOutcome(action="exhausted", operator_note=note)
                note = takeover or note                # prefer the note captured at Done
                outcome = human_outcome = "takeover_resume"
                self._takeover_active = True
                try:
                    after = await self._dom_snapshot()  # re-read DOM AFTER the human is done (ADR-7)
                    dom_diff = summarize(before, after)
                finally:
                    self._takeover_active = False       # never leave the flag stuck True (recorder relies on it)
                await self._drive(_DRIVE_STATE, "running")
            elif action == "abort":
                outcome = human_outcome = "abort"
                await self._drive(_DRIVE_STATE, "blocked")
            else:  # resume
                outcome = human_outcome = "resume"
                await self._drive(_DRIVE_STATE, "running")
        finally:
            write_escalation_event(self.evidence_dir, step_id=context.step_id, reason=context.reason,
                                   human_outcome=human_outcome, duration_s=time.time() - t0,
                                   dom_diff_summary=dom_diff, operator_note=note)
        return EscalationOutcome(action=outcome, operator_note=note)

    async def _await_takeover_done(self, context: EscalationContext) -> "str | None":
        """Bug-1 fix (user-approved Option 2): after 'Take over & resume', switch the panel to PLANNED mode
        (prompt + single Done) and block until the human clicks Done. Returns the Done note (possibly "") on
        completion, or None if the wait timed out. Reuses the existing planned-mode panel machinery.

        The whole point of taking over is to ACT in the browser — often by navigating. `_wait_persisting`
        re-applies PLANNED mode on every page `load` so the Done button survives navigation and the human is
        never stranded (Gap-A fix, shared with escalate_planned/escalate)."""
        planned_ctx = {"prompt": _TAKEOVER_PROMPT, "reason": _TAKEOVER_REASON,
                       "capability": self.capability_name, "hint": context.hint}
        self._bridge.reset()
        await self._drive(_DRIVE_PLANNED, planned_ctx)
        print(f"[escalation] take-over at step '{context.step_id}' — you are in control; act in the in-browser "
              f"panel then click Done. Auto-exhausts after {_REACTIVE_TIMEOUT_S}s.", flush=True)
        try:
            payload = await self._wait_persisting(_DRIVE_PLANNED, planned_ctx, _REACTIVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
        return payload.get("note") or ""

    async def escalate_planned(self, prompt: str, reason: str, timeout_ms: int = 60000,
                               hint: "str | None" = None) -> None:
        """Planned intervention (ADR-007 planned mode): overlay shows the prompt + a single Done button; block
        until the human clicks Done or the timeout elapses. `hint` (ADR-007 revision) is shown in the panel's
        "About this step" section. Void return (the human acts on the page; no value flows back). Raises
        asyncio.TimeoutError on timeout -> the executor turns that into a hard_failure."""
        await self.install()
        self._bridge.reset()
        t0 = time.time()
        before = await self._dom_snapshot()
        planned_ctx = {"prompt": format_human_prompt(prompt), "reason": reason,
                       "capability": self.capability_name, "hint": hint}
        await self._drive(_DRIVE_PLANNED, planned_ctx)
        # exactly ONE terminal info line; the prompt itself is shown VERBATIM in the in-browser panel only.
        print(f"[escalation] awaiting human input (planned, reason={reason}) — follow the prompt in the "
              f"in-browser panel and click Done. Times out after {int(timeout_ms / 1000)}s.", flush=True)
        outcome = "planned_done"
        dom_diff = None
        note = None
        try:
            # Gap-A: keep the planned panel visible even if the human's action navigates the page.
            payload = await self._wait_persisting(_DRIVE_PLANNED, planned_ctx, timeout_ms / 1000.0)
            note = payload.get("note")
            self._takeover_active = True                # observation suspended during planned intervention
            try:
                after = await self._dom_snapshot()      # re-read DOM before the next step (ADR-7)
                dom_diff = summarize(before, after)
            finally:
                self._takeover_active = False
            await self._drive(_DRIVE_STATE, "running")
        except asyncio.TimeoutError:
            outcome = "timeout"
            self._takeover_active = False
            await self._drive(_DRIVE_STATE, "blocked")
            raise
        finally:
            write_escalation_event(self.evidence_dir, step_id="(planned)", reason=reason,
                                   human_outcome=outcome, duration_s=time.time() - t0,
                                   dom_diff_summary=dom_diff, operator_note=note)
