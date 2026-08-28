"""Always-on evidence-layer screenshot capture (ADR-004 evidence clause).

This is DISTINCT from the LLM-observation screenshot (which is src/agent/'s concern). It fires on genuine
failure categories only: locator exhaustion/ambiguity, checkpoint timeout, and find_matching exhaustion.
Destination is injected (PlaywrightExecutor(evidence_dir=...)) — never hardcoded. Filename pattern mirrors
the `generate: unique_string` marker for consistency: `evidence_<timestamp>_<random_suffix>.png`.
"""
from __future__ import annotations

import datetime
import logging
import secrets
from pathlib import Path

_log = logging.getLogger("executor.evidence")


class EvidenceCapture:
    def __init__(self, page, evidence_dir: Path) -> None:
        self._page = page
        self._dir = Path(evidence_dir)
        # Declaration-driven region masking (§3.4): Playwright overlays an opaque box over each of these
        # locators in every evidence screenshot, so the value of a `sensitive: true`-bound input is obscured
        # while page structure and error text stay debuggable. Populated by the executor from the artifact's
        # sensitive-bound steps at replay start; empty during discovery (sensitivity isn't marked until
        # emission) and in unit tests.
        self.mask_locators: list = []

    async def capture(self, label: str = "failure") -> str:
        """Screenshot the current page to the evidence dir; return the file path as a string. Any registered
        `mask_locators` are overlaid so sensitive-bound values never land in the image."""
        self._dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        suffix = secrets.token_hex(2)  # 4 hex chars, same shape as generate: unique_string
        path = self._dir / f"evidence_{ts}_{suffix}.png"
        kwargs = {"path": str(path)}
        if self.mask_locators:
            kwargs["mask"] = self.mask_locators
            kwargs["mask_color"] = "#000000"
        try:
            await self._page.screenshot(**kwargs)
        except Exception as exc:  # never let evidence capture mask the underlying failure
            _log.warning("evidence screenshot failed (%s): %s", label, exc)
        else:
            _log.info("evidence screenshot (%s) -> %s", label, path)
        return str(path)
