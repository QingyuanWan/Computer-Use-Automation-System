"""Auto-replay validation gate (ADR-006 β + ADR-9).

After discovery (+ injection) completes, replay the emitted artifact against a FRESH browser session to
confirm it works deterministically. Validation data comes from `metadata.sample_invocation` — the
caller_parameters used at discovery, referencing pre-existing registry data (ADR-9) — NOT from per-invocation
fixture composition (removed). This module delegates replay entirely to the existing src/replay ReplayEngine;
it only (1) stands up a clean executor, (2) navigates to the artifact's implicit start URL, (3) resolves the
replay parameters from sample_invocation, and (4) reports the result so the caller can flip metadata.validated.
"""
from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import Optional

from src.executor import PlaywrightExecutor
from src.models import Artifact
from src.registry import resolve_sample_invocation  # $json:<dot.path> refs → JSON registry (single source)
from src.replay import ReplayEngine, ReplayResult, StubEscalationHandler

_log = logging.getLogger("agent.validation_gate")

# resolve_sample_invocation (imported from src.registry) turns "$json:dot.path" references in a
# sample_invocation into current literals from test_data/parabank_credentials.json; re-exported here so it is
# importable as `src.agent.validation_gate.resolve_sample_invocation` too.


def _no_fixture_loader(name: str) -> Artifact:
    # ADR-9 removed fixture composition; the loader seam is retained by ReplayEngine but never invoked.
    raise KeyError(f"fixture composition is not supported (ADR-9); no loader for '{name}'")


def _origin(url: str) -> Optional[str]:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else None


async def run_validation_gate(artifact: Artifact, *, target_url: str, evidence_dir,
                              headless: bool = True, escalation_handler=None) -> ReplayResult:
    """Replay `artifact` in a fresh PlaywrightExecutor context (never the discovery session), using
    metadata.sample_invocation as caller_parameters (ADR-9). Returns the ReplayEngine's ReplayResult; the
    caller flips validated=true only on status == 'success'.

    If the artifact declares parameters but has no sample_invocation, the gate cannot execute deterministically
    and returns a hard_failure with a clear reason (ADR-9)."""
    has_params = artifact.parameters is not None and bool(artifact.parameters.properties)
    # Resolve $json:<dot.path> references against the JSON credential registry so validation uses the live
    # registry value — not the discovery-time literal.
    caller_parameters, missing = resolve_sample_invocation(artifact.metadata.sample_invocation)
    if missing:
        reason = f"credential_path_missing:{','.join(missing)}"
        _log.warning("validation gate: sample_invocation references missing credential path(s): %s", missing)
        return ReplayResult.hard_failure(reason=reason)
    if has_params and not caller_parameters:
        reason = ("artifact declares parameters but has no metadata.sample_invocation; validation cannot "
                  "execute without caller_parameters (ADR-9)")
        _log.warning("validation gate: %s", reason)
        return ReplayResult.hard_failure(reason=reason)

    executor = PlaywrightExecutor(evidence_dir=Path(evidence_dir) / "validation",
                                  base_url=_origin(target_url), headless=headless)
    await executor.start()
    try:
        # The discovered artifact's steps assume the page is already on target_url (the initial goto happens
        # OUTSIDE the recorded steps during discovery), so put the fresh session there before replaying.
        await executor.page.goto(target_url)
        # NOTE: the unattended gate is headless; a REAL reactive handler here would block forever with no
        # human. human_input capabilities are already skipped upstream, so the injected handler is only for
        # parity — reactive escalation during a well-formed validation replay is not expected.
        engine = ReplayEngine(executor, escalation_handler or StubEscalationHandler(), _no_fixture_loader)
        result = await engine.replay(artifact, caller_parameters=caller_parameters)
        _log.info("validation gate: replay status=%s", result.status)
        return result
    finally:
        await executor.stop()
