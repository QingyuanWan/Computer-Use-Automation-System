"""interface.ai CLI — the §6 demo path. Two subcommands wire every module into the record-once/replay-many
flow:

    python -m src.cli discover --goal "..." --target-url "..." --capability-name NAME \
        --caller-params-from-json account_id=primary.checking_id
    python -m src.cli replay --artifact-name NAME \
        --caller-params-from-json account_id=primary.checking_id

Wiring (per docs/phase2_ab_review.md): the real PlaywrightEscalationHandler is injected into discovery,
validation gate, and replay via `page_provider=executor.get_current_page`; DiscoveryAgent takes it as a
param. `.env` is loaded at startup (ONLY for ANTHROPIC_API_KEY); discover fails fast if ANTHROPIC_API_KEY is
missing; caller params are resolved from the JSON credential registry (`KEY=json.dot.path`, e.g.
`account_id=primary.checking_id`) with a fail-fast if the store or the path is missing (single-source
refactor — there is NO env-var credential path). The CLI is ALWAYS headed (ADR-7/8:
headed is the takeover-capable mode; there is no headless CLI knob — `--headed` is a deprecated no-op); the
in-browser overlay is where a human sees the escalation prompt and Resume/Take-over/Abort or planned-Done
buttons. Every escalation wait is bounded (planned: the step's timeout_ms; reactive: 5 min) and Ctrl+C
cleanly cancels + closes the browser. Summaries print a whitelist of non-secret fields to stdout and to
`<evidence-dir>/cli_summary.json`.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

from src import registry
from src.models import CapabilityType
from src.safety import SafetyGate, load_policy
from src.agent import DiscoveryAgent
from src.agent.config import AgentConfigError, load_api_key
from src.escalation import PlaywrightEscalationHandler
from src.executor import PlaywrightExecutor
from src.replay import ReplayEngine
from src.storage import ArtifactNotFoundError, ArtifactStorage


def _origin(url: str) -> str:
    p = urllib.parse.urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else url


def _explain_hard_failure(reason: "str | None") -> str:
    """One-line gloss for the four Phase-3 hard_failure subtypes (D6): stub_unavailable / human_aborted /
    escalation_exhausted / technical_error — for the replay operator."""
    r = reason or ""
    if r == "stub_unavailable":
        return ("subtype=stub_unavailable — a stuck condition needed a human but none was reachable "
                "(no interactive escalation handler)")
    if r == "human_aborted":
        return "subtype=human_aborted — a human was escalated to and chose Abort"
    if r == "escalation_exhausted" or r.startswith("escalation_exhausted:"):
        detail = r.split(":", 1)[1] if ":" in r else ""
        return ("subtype=escalation_exhausted — escalated to a human but the stuck condition still could not "
                f"be resolved{f' ({detail})' if detail else ''}")
    if r.startswith("technical_error:"):
        return f"subtype=technical_error — unresolvable technical failure ({r.split(':', 1)[1]})"
    if r == "safety_blocked:mutating_requires_consent":
        return ("subtype=safety_blocked — this is a MUTATING capability that changes real state on every "
                "replay; it was refused. Re-run with --i-understand-mutating to consent.")
    if r.startswith("safety_blocked:"):
        return (f"subtype=safety_blocked — a runtime guardrail refused the replay "
                f"({r.split(':', 1)[1]}); terminal, not escalated")
    return f"subtype=other ({r})" if r else "subtype=other (no reason recorded)"


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _resolve_caller_params(pairs: "list[str] | None") -> dict[str, str]:
    """Turn ['account_id=primary.checking_id', ...] into {'account_id': <value from the JSON registry>}.
    Fails fast on a malformed pair, a missing/blank JSON store, or a dot-path absent from the store. The JSON
    credential registry is the ONLY credential source (no env-var fallback)."""
    if not pairs:
        return {}
    try:
        creds = registry.require_credentials()
    except registry.CredentialError as exc:
        sys.exit(f"error: {exc}")
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"error: --caller-params-from-json expects KEY=JSON.PATH, got {pair!r}")
        key, path = pair.split("=", 1)
        try:
            out[key.strip()] = registry.resolve_ref(path.strip(), creds)
        except registry.CredentialError as exc:
            sys.exit(f"error: {exc}")
    return out


def _caller_param_sources(pairs: "list[str] | None") -> dict[str, str]:
    """The registry-reference form for sample_invocation: {'account_id': '$json:primary.checking_id'}. Lets the
    validation gate (and the launcher) re-resolve the live registry value from the JSON store at replay time."""
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" in pair:
            key, path = pair.split("=", 1)
            out[key.strip()] = f"$json:{path.strip()}"
    return out


def _write_summary(evidence_dir: Path, summary: dict) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "cli_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _rel_evidence(evidence_dir: Path) -> str:
    """A repo-relative, forward-slash evidence path so a summary is portable and both entry points serialize it
    identically (the launcher's default evidence base is absolute, the raw CLI's is relative — normalize both,
    and keep the machine-absolute prefix out of the shipped record)."""
    ed = evidence_dir.resolve()
    try:
        ed = ed.relative_to(Path.cwd())
    except ValueError:
        pass  # a custom --evidence-dir outside the cwd: fall back to the absolute path
    return ed.as_posix()


def _write_replay_summary(evidence_dir: Path, capability_name: str, result) -> None:
    """§3.5 structured record on EVERY terminal replay outcome (success / business_outcome / hard_failure).
    Written here in the shared core so BOTH entry points — `python -m src.cli replay` AND
    scripts/run_capability.py — leave an on-disk record; previously only the raw-CLI handler wrote it, so the
    README's launcher path left the happy path and the legitimate business-result path with no trace."""
    _write_summary(evidence_dir, {
        "command": "replay", "capability_name": capability_name, "status": result.status,
        "outcome_name": result.outcome_name, "reason": result.reason, "outputs": result.outputs,
        "operator_note": result.operator_note, "evidence_dir": _rel_evidence(evidence_dir),
    })


# ---------------- reusable cores (shared by the subcommands AND scripts/run_capability.py) ----------------
# These return the typed result objects (no printing) so the friendly launcher can inspect the outcome
# (e.g. to decide whether to auto-reseed). The argparse handlers below wrap them with printing + summaries.

async def replay_capability(artifact, *, target_url: str, caller_params: dict, evidence_dir: Path,
                            artifact_loader, interactive: bool = True, allow_mutating: bool = False):
    """Core replay wiring. Returns a ReplayResult. `interactive=False` (used by run_capability's default,
    unattended replay) maps stuck conditions to hard_failure(stub_unavailable) instead of opening an in-browser
    panel that an unattended evaluator would never click (avoids the 5-min reactive-timeout hang).
    `allow_mutating` (D1 fix): a `mutating` capability is blocked at pre-flight unless this is True — set by the
    CLI/launcher's `--i-understand-mutating` flag. This is the single shared boundary both entry points
    (`python -m src.cli replay` AND scripts/run_capability.py) funnel through, so both are enforced here."""
    cap = artifact.metadata.capability_name
    gate = SafetyGate(policy=load_policy(), allow_mutating=allow_mutating)   # §3.4 configurable allowlist:
    # load_policy() reads safety_policy.json if present, else the in-code PARABANK_POLICY (ADR-008 §Safety).
    executor = PlaywrightExecutor(evidence_dir=evidence_dir, base_url=_origin(target_url), headless=False,
                                  safety_gate=gate)
    await executor.start()
    try:
        handler = PlaywrightEscalationHandler(page_provider=executor.get_current_page,
                                              evidence_dir=evidence_dir, capability_name=cap,
                                              interactive=interactive)
        await executor.page.goto(target_url)      # artifact's implicit start state
        engine = ReplayEngine(executor, handler, artifact_loader, safety_gate=gate)
        result = await engine.replay(artifact, caller_parameters=caller_params)
        _write_replay_summary(evidence_dir, cap, result)   # §3.5: record on every terminal outcome, both entry points
        # §3.5: a masked final-state screenshot on the non-failure terminal outcomes (a hard_failure already
        # captured one at the failing step). The engine registered the sensitive-region masks at replay start,
        # so this reuses the same declaration-driven masking seam (F3a) — the raw page is never shot un-masked.
        if result.status in ("success", "business_outcome") and executor.evidence is not None:
            await executor.evidence.capture(f"terminal_{result.status}")
        return result
    finally:
        await executor.stop()


async def discover_capability(*, goal: str, target_url: str, capability_name: str, capability_type: CapabilityType,
                              caller_params: dict, caller_sources: dict, evidence_dir: Path, max_steps: int = 20,
                              skip_validation: bool = False, interactive: bool = True, state_fingerprint=None):
    """Core discovery wiring. Returns a DiscoveryResult (caller saves the artifact + prints). `state_fingerprint`
    (Slice 1d) is an optional injected opaque state-snapshot provider for the declared-read state-delta check;
    None (the `python -m src.cli` path) skips the check and records the read as unverified — the launcher injects
    the ParaBank one. Kept as an injected callable so the agent stays surface-agnostic (REPORT §4)."""
    executor = PlaywrightExecutor(evidence_dir=evidence_dir, base_url=_origin(target_url), headless=False)
    await executor.start()
    try:
        handler = PlaywrightEscalationHandler(page_provider=executor.get_current_page,
                                              evidence_dir=evidence_dir, capability_name=capability_name,
                                              interactive=interactive)
        # §3.4: allowlist check_action during discovery (policy from safety_policy.json if present). Slice 1d:
        # inject the state-fingerprint provider; the agent compares its opaque snapshots, never inspects them.
        agent = DiscoveryAgent(executor, escalation_handler=handler, evidence_root=evidence_dir,
                               max_steps=max_steps, safety_gate=SafetyGate(policy=load_policy()),
                               state_fingerprint=state_fingerprint)
        if skip_validation:
            return await agent.discover(goal, target_url, capability_name, capability_type=capability_type,
                                        caller_parameters=caller_params, caller_parameter_sources=caller_sources)
        return await agent.discover_and_validate(goal, target_url, capability_name,
                                                 capability_type=capability_type,
                                                 caller_parameters=caller_params,
                                                 caller_parameter_sources=caller_sources,
                                                 validation_headless=True)
    finally:
        await executor.stop()


# ---------------- discover ----------------

async def _discover(args) -> int:
    caller_params = _resolve_caller_params(args.caller_params_from_json)     # literals (for the LLM)
    caller_sources = _caller_param_sources(args.caller_params_from_json)     # $json: refs (for sample_invocation)
    try:
        load_api_key()                       # fail fast: discovery needs the LLM
    except AgentConfigError as exc:
        sys.exit(f"error: {exc}")

    evidence_dir = Path(args.evidence_dir)
    result = await discover_capability(                # ADR-7/8: always headed (takeover-capable)
        goal=args.goal, target_url=args.target_url, capability_name=args.capability_name,
        capability_type=CapabilityType(args.capability_type),
        caller_params=caller_params, caller_sources=caller_sources, evidence_dir=evidence_dir,
        max_steps=args.max_steps, skip_validation=args.skip_validation, interactive=True)

    if result.artifact is None:
        print(f"[discover] FAILED — status={result.status} detail={result.detail}")
        _write_summary(evidence_dir, {"command": "discover", "status": result.status, "artifact": None})
        return 1

    storage = ArtifactStorage(Path(args.artifacts_dir))
    saved = storage.save(result.artifact)     # save-with-warning policy (ADR-6): keep it, flag if unvalidated
    m = result.artifact.metadata
    warnings = list(getattr(result, "warnings", []) or [])
    summary = {
        "command": "discover", "status": result.status, "capability_name": m.capability_name,
        "capability_type": m.capability_type.value, "validated": m.validated,
        "state_verified": m.state_verified,
        "validation_status": result.validation_status, "validation_skip_reason": m.validation_skip_reason,
        "sample_invocation": m.sample_invocation, "expected_outcomes_added": result.expected_outcomes_added,
        "total_billed_tokens": result.total_billed_tokens, "artifact_path": str(saved),
        "evidence_dir": result.evidence_dir, "warnings": warnings,
    }
    print(f"[discover] status={result.status} capability={m.capability_name} type={m.capability_type.value}")
    print(f"           validated={m.validated} state_verified={m.state_verified} "
          f"validation={result.validation_status} skip_reason={m.validation_skip_reason}")
    print(f"           sample_invocation={m.sample_invocation} billed_tokens={result.total_billed_tokens}")
    print(f"           saved -> {saved}")
    # Surface run-time warnings (e.g. an unverified read: state_verified=null because no fingerprint provider
    # was injected) at the moment the artifact is produced — not only on later inspection of the metadata.
    for w in warnings:
        print(f"[discover] WARNING: {w}")
    if not m.validated:
        print(f"[discover] WARNING: validated=false — NOT delivery-ready (ADR-6). "
              f"(validation={result.validation_status})")
    _write_summary(evidence_dir, summary)
    return 0


# ---------------- replay ----------------

async def _replay(args) -> int:
    caller_params = _resolve_caller_params(args.caller_params_from_json)
    storage = ArtifactStorage(Path(args.artifacts_dir))
    try:
        artifact = (storage.load_from_path(Path(args.artifact_file)) if args.artifact_file
                    else storage.load(args.artifact_name))
    except ArtifactNotFoundError as exc:
        sys.exit(f"error: {exc}")

    cap = artifact.metadata.capability_name
    target_url = args.target_url or (registry.load_credentials() or {}).get("url")
    if not target_url:
        sys.exit("error: replay needs a start URL — pass --target-url or seed the JSON registry "
                 "(python scripts/seed_parabank_accounts.py)")

    evidence_dir = Path(args.evidence_dir) / f"{cap}_replay_{_ts()}"
    result = await replay_capability(artifact, target_url=target_url, caller_params=caller_params,
                                     evidence_dir=evidence_dir, artifact_loader=storage.load,
                                     interactive=True,   # headed CLI ⇒ a human is present at the panel
                                     allow_mutating=getattr(args, "i_understand_mutating", False))

    # cli_summary.json is written by the shared replay_capability core (both entry points), not here.
    print(f"[replay] capability={cap} status={result.status}")
    if result.status == "success":
        print(f"         outputs={result.outputs}")
    elif result.status == "business_outcome":
        print(f"         outcome={result.outcome_name} observed={result.observed_text!r}")
    else:  # hard_failure -> decode the Phase-3 subtype (D6)
        print(f"         {_explain_hard_failure(result.reason)}")
        if result.operator_note:
            print(f"         operator_note={result.operator_note!r}")
    # A business_outcome (e.g. account_not_found) is a legitimate result, not a crash — it exits 0, matching
    # scripts/run_capability.py. Only a hard_failure is non-zero. (Both entry points now agree.)
    return 0 if result.status in ("success", "business_outcome") else 1


# ---------------- argparse ----------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.cli", description="interface.ai record-once/replay-many CLI")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="LLM-drive a goal on a live surface, emit + validate a capability")
    d.add_argument("--goal", required=True)
    d.add_argument("--target-url", required=True)
    d.add_argument("--capability-name", required=True)
    d.add_argument("--capability-type", required=True, choices=["read", "mutating"],
                   help="REQUIRED safety label declared by the caller (Slice 1): 'read' (no state change; "
                        "eligible for failure-injection) or 'mutating' (changes real state; needs "
                        "--i-understand-mutating at replay). No default — a safety label must be deliberate. "
                        "A capability with a human_input step is forced to 'mutating' regardless.")
    d.add_argument("--caller-params-from-json", nargs="*", metavar="KEY=JSON.PATH", default=[],
                   help="map caller params to dot-paths in test_data/parabank_credentials.json "
                        "(e.g. account_id=primary.checking_id)")
    d.add_argument("--artifacts-dir", default="artifacts")
    d.add_argument("--evidence-dir", default="evidence")
    d.add_argument("--max-steps", type=int, default=20)
    d.add_argument("--headed", action="store_true",
                   help="(deprecated no-op) the CLI is ALWAYS headed per ADR-7/8 — there is no headless mode")
    d.add_argument("--skip-validation", action="store_true", help="emit without the auto-replay validation gate")

    r = sub.add_parser("replay", help="deterministically replay a saved capability (no LLM)")
    r.add_argument("--artifact-name")
    r.add_argument("--artifact-file")
    r.add_argument("--caller-params-from-json", nargs="*", metavar="KEY=JSON.PATH", default=[],
                   help="map caller params to dot-paths in test_data/parabank_credentials.json "
                        "(e.g. account_id=primary.checking_id)")
    r.add_argument("--target-url", help="start URL (defaults to the 'url' in the JSON registry)")
    r.add_argument("--artifacts-dir", default="artifacts")
    r.add_argument("--evidence-dir", default="evidence")
    r.add_argument("--i-understand-mutating", action="store_true",
                   help="consent to replay a MUTATING capability (changes real state every run, e.g. "
                        "transfer_funds/request_loan). Without it a mutating replay is blocked (D1 safety gate).")
    r.add_argument("--headed", action="store_true",
                   help="(deprecated no-op) the CLI is ALWAYS headed per ADR-7/8 — there is no headless mode")
    return p


def main(argv: "list[str] | None" = None) -> int:
    load_dotenv()          # ONLY for ANTHROPIC_API_KEY; ParaBank creds come from the JSON registry
    args = _build_parser().parse_args(argv)
    if args.command == "replay" and not (args.artifact_name or args.artifact_file):
        sys.exit("error: replay needs --artifact-name or --artifact-file")
    handler = {"discover": _discover, "replay": _replay}[args.command]
    try:
        return asyncio.run(handler(args))
    except KeyboardInterrupt:
        # asyncio.run cancels the running task on Ctrl+C; the _discover/_replay `finally` closes the browser
        # as it unwinds, so this just reports a clean exit (130 = SIGINT). The bounded, sliced escalation wait
        # (bridge.py) is what lets the interrupt reach here promptly instead of being swallowed on Windows.
        print("\n[interrupted] cancelled by user (Ctrl+C); browser closed.", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
