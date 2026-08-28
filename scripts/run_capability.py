#!/usr/bin/env python
"""run_capability.py — the evaluator-friendly launcher (thin wrapper over the src.cli cores).

Modes:

  # List bundled capabilities and exit (no browser, no credentials):
  python scripts/run_capability.py --list-capabilities

  # Default — REPLAY a bundled capability. No API key needed. ParaBank test creds come from
  # test_data/parabank_credentials.json (auto-seeded on first run / when stale).
  python scripts/run_capability.py --artifact-name lookup_checking_balance

  # Discover-then-replay. Needs ANTHROPIC_API_KEY in .env.
  python scripts/run_capability.py --goal "..." --capability-name my_cap \
      --caller-params-from-json username=primary.username ...

Credential storage: `.env` holds ONLY the evaluator's ANTHROPIC_API_KEY. System-managed ParaBank test
accounts live in test_data/parabank_credentials.json (gitignored) — the SINGLE credential source of truth.
The launcher loads that JSON and resolves an artifact's `sample_invocation` `$json:<dot.path>` references
directly against it. There is NO env-var credential path (the old `$env:`/`--caller-params-from-env` bridge
was removed in the single-source refactor; see docs/env_removal_review.md).

Pre-flight credential verification (Option C — deterministic): ParaBank purges its sandbox accounts every
~30-60 min. `_verify_credentials()` runs ONCE at the very start of every command (replay, discover, and
--show-accounts), BEFORE any browser/LLM work: a tiny headless login check; if the seeded creds no longer
authenticate it reseeds a fresh account, rewrites the JSON, and verifies the new account authenticates — or
exits cleanly if ParaBank is down. This replaced the old fragile approaches (pattern-matching the replay's
error text, and a mid-replay "secondary net"), which mis-fired because a stale login surfaces
non-deterministically. The keyword classifier `is_login_failure` is retained only as a diagnostic utility; it
no longer triggers any reseed.

Boundary: this file does NOT reimplement CLI logic — it delegates to src.cli.replay_capability /
discover_capability (the same wiring the `python -m src.cli` subcommands use), to src.registry (via
scripts.credentials) for JSON storage/resolution, and to scripts.seed_parabank_accounts._seed for reseeding.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent          # scripts/
_ROOT = _HERE.parent                             # repo root
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from scripts import credentials  # noqa: E402
from scripts.seed_parabank_accounts import _seed  # noqa: E402
from src.agent.config import AgentConfigError, load_api_key  # noqa: E402
from src.cli import (  # noqa: E402
    _caller_param_sources,
    _resolve_caller_params,
    _ts,
    discover_capability,
    replay_capability,
)
from src.models import CapabilityType  # noqa: E402
from src.storage import ArtifactNotFoundError, ArtifactStorage  # noqa: E402

_ENV_PATH = _ROOT / ".env"                       # evaluator-provided data only (ANTHROPIC_API_KEY)
_DEFAULT_ARTIFACTS = _ROOT / "artifacts"
_DEFAULT_EVIDENCE = _ROOT / "evidence"
_DEFAULT_URL = "https://parabank.parasoft.com/parabank"

# Login / stale-credential signatures. Auto-reseed fires ONLY when a hard_failure(technical_error) message
# contains one of these — NOT on unrelated technical errors (Playwright crash, network drop) and NOT on other
# outcomes. Includes ParaBank's *empirically observed* stale-session texts (feasibility probe, context.md):
# a purged username → "The username and password could not be verified"; a not-logged-in protected page →
# "An internal error has occurred"; a stale account id → "Could not find account #...".
_RESEED_KEYWORDS = (
    "login", "log in", "credential", "invalid user", "authentication", "not authorized", "unauthorized",
    "could not be verified", "username and password", "internal error", "could not find account",
    "please log in", "session", "expired",
)


# ---------------- mode + result helpers (unit-tested) ----------------

def mode_for(args) -> str:
    """'discover' when a --goal is supplied (discover-then-replay), else 'replay' (bundled artifact)."""
    return "discover" if getattr(args, "goal", None) else "replay"


def is_login_failure(result) -> bool:
    """True only for a hard_failure whose technical_error reason contains a login/stale-credential keyword.
    Retained as a diagnostic classifier (unit-tested); it is NO LONGER the reseed trigger — credential
    freshness is now guaranteed up front by the deterministic `_verify_credentials` pre-flight (Option C),
    which replaced the fragile mid-flow keyword detection."""
    if getattr(result, "status", None) != "hard_failure":
        return False
    reason = (getattr(result, "reason", None) or "")
    if not reason.startswith("technical_error"):
        return False
    low = reason.lower()
    return any(kw in low for kw in _RESEED_KEYWORDS)


def caller_params_from_sample_invocation(artifact, creds: dict) -> dict:
    """Resolve an artifact's metadata.sample_invocation into caller params against the JSON registry.
    `$json:<dot.path>` refs are resolved from `creds` (so a post-reseed re-resolve picks up fresh values);
    anything else is used literally. Raises CredentialError if a referenced path is absent from the store."""
    si = getattr(artifact.metadata, "sample_invocation", None) or {}
    resolved, missing = credentials.resolve_sample_invocation(si, creds)
    if missing:
        raise credentials.CredentialError(
            f"artifact references credential path(s) {missing} not in {credentials.CRED_PATH.name} — "
            f"run: python scripts/seed_parabank_accounts.py")
    return resolved


# ---------------- credentials (JSON) + reseed ----------------

async def do_reseed() -> dict:
    """Register fresh ParaBank test accounts and write them to test_data/parabank_credentials.json. Raises on
    failure (ParaBank down) — the caller surfaces it, no endless retry."""
    seed = await _seed()                          # {PARABANK_PRIMARY_USERNAME/PASSWORD/CHECKING_ID/SAVINGS_ID}
    creds = credentials.from_seed_dict(seed)
    credentials.save_credentials(creds)
    return creds


async def ensure_credentials() -> dict:
    """Return usable ParaBank credentials from test_data/parabank_credentials.json (the single source of
    truth), seeding fresh accounts on first run / clean state. There is NO env-var fallback."""
    creds = credentials.load_credentials()
    if creds is None:
        print("[run_capability] no ParaBank test credentials found — seeding fresh accounts (one-time) ...")
        creds = await do_reseed()
    return creds


async def login_ok(creds: dict) -> bool:
    """Deterministic pre-flight (Fix 2): do the current primary credentials actually authenticate against
    ParaBank? This REPLACES the brittle error-text keyword match, which missed the non-deterministic stale-login
    subtype — a purged account surfaces at replay as EITHER hard_failure(technical_error) OR
    hard_failure(stub_unavailable), so keying off the reason string was unreliable. Returns True if the check
    itself can't run (don't force a reseed on an unrelated error).

    Per-ACCOUNT check (not just "is the logged-in chrome present"): after logging in we open the
    session-gated overview and assert OUR OWN checking_id appears. The old heuristic ("log out" / "accounts
    overview" in the page body) is defeated by the ParaBank public sandbox, which now serves a SHARED
    'john smith' demo session (with its own account-overview table) even after a FAILED login with bogus
    creds — so that text is present regardless of whether our credentials authenticated, and the pre-flight
    false-passed for stale accounts (auto-reseed never fired). Our seeded checking_id is unique to our
    account and only shows in our own overview, so it is an unambiguous per-account authentication signal."""
    url = creds.get("url", credentials.DEFAULT_URL).rstrip("/")
    p = creds.get("primary", {})
    checking_id = str(p.get("checking_id") or "")
    if not p.get("username") or not checking_id:
        return False
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(f"{url}/index.htm", wait_until="domcontentloaded")
                await page.fill('input[name="username"]', p.get("username", ""))
                await page.fill('input[name="password"]', p.get("password", ""))
                await page.click('input[value="Log In"]')
                await page.wait_for_load_state("networkidle")
                # session-gated: overview only lists the *authenticated* user's accounts
                await page.goto(f"{url}/overview.htm", wait_until="networkidle")
                body = await page.locator("body").inner_text()
                return checking_id in body
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001 - a failed CHECK must not force a reseed loop
        print(f"[run_capability] login pre-check could not run ({exc}); proceeding without pre-flight reseed")
        return True


# ---------------- --show-accounts (diagnostic, read-only) ----------------

async def fetch_accounts(creds: dict) -> list[dict]:
    """Log in and read each account's (id, type, balance, available) from the overview + activity pages.
    Read-only diagnostic — NO capability logic. Reuses the same login step pattern as login_ok()."""
    url = creds.get("url", credentials.DEFAULT_URL).rstrip("/")
    p = creds.get("primary", {})
    accounts: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(f"{url}/index.htm", wait_until="domcontentloaded")
            await page.fill('input[name="username"]', p.get("username", ""))
            await page.fill('input[name="password"]', p.get("password", ""))
            await page.click('input[value="Log In"]')
            await page.wait_for_load_state("networkidle")
            await page.goto(f"{url}/overview.htm", wait_until="networkidle")
            for row in await page.locator("#accountTable tr").all_inner_texts():
                cells = [c.strip() for c in row.split("\t")]
                if len(cells) < 3 or not cells[0].isdigit():   # skip header + the Total row
                    continue
                accounts.append({"id": cells[0], "type": "?", "balance": cells[1], "available": cells[2]})
            for a in accounts:                                  # type lives on the per-account activity page
                await page.goto(f"{url}/activity.htm?id={a['id']}", wait_until="networkidle")
                text = await page.locator("#rightPanel").inner_text()
                m = re.search(r"Account Type:\s*([A-Za-z]+)", text)
                if m:
                    a["type"] = m.group(1).upper()
        finally:
            await browser.close()
    return accounts


def _print_accounts_table(creds: dict, accounts: list[dict]) -> None:
    print(f"ParaBank accounts for {creds.get('primary', {}).get('username', '?')}:")
    print(f"  {'Account':<10}{'Type':<11}{'Balance':<12}{'Available':<12}")
    for a in accounts:
        print(f"  {a['id']:<10}{a['type']:<11}{a['balance']:<12}{a['available']:<12}")
    if not accounts:
        print("  (no accounts found)")


async def _show_accounts(args) -> int:
    """--show-accounts: log in and print the ParaBank test accounts, then exit. Uses the same pre-flight
    credential verification as replay/discover (verify -> reseed-if-stale -> verify)."""
    try:
        creds = await _verify_credentials()
    except PreflightError as exc:
        print(f"[run_capability] {exc}")
        return 1
    try:
        accounts = await fetch_accounts(creds)
    except Exception as exc:  # noqa: BLE001
        print(f"[run_capability] could not read accounts: {exc}")
        return 1
    _print_accounts_table(creds, accounts)
    return 0


# ---------------- listing (for --help and --list-capabilities) ----------------

def list_bundled_artifacts(artifacts_dir: Path = _DEFAULT_ARTIFACTS) -> str:
    """A human-readable list of bundled artifacts (capability_name — capability_type — description) for the
    --help epilog and the --list-capabilities flag. `description` falls back to target_app_hint or '—' since
    the schema has no dedicated description field (schema-draft §2)."""
    if not artifacts_dir.is_dir():
        return "Bundled capabilities: (none found)"
    rows = []
    for p in sorted(artifacts_dir.glob("*.yaml")):
        try:
            meta = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("metadata", {})
            name = meta.get("capability_name", p.stem)
            ctype = meta.get("capability_type", "?")
            desc = meta.get("description") or meta.get("target_app_hint") or "—"
            rows.append(f"  {name} — {ctype} — {desc}")
        except Exception:  # noqa: BLE001 - a malformed artifact must not break --help
            rows.append(f"  {p.stem} — (unreadable)")
    return "Bundled capabilities (use with --artifact-name):\n" + ("\n".join(rows) if rows else "  (none)")


def _print_result(result) -> None:
    status = getattr(result, "status", "?")
    if status == "success":
        print(f"[run_capability] SUCCESS — outputs={getattr(result, 'outputs', {})}")
    elif status == "business_outcome":
        print(f"[run_capability] BUSINESS OUTCOME — {getattr(result, 'outcome_name', '?')} "
              f"(a legitimate non-crash result)")
    elif getattr(result, "reason", None) == "safety_blocked:mutating_requires_consent":
        print("[run_capability] REFUSED — this is a MUTATING capability that changes real state on every "
              "replay.\n                 Re-run with --i-understand-mutating to consent (D1 safety gate).")
    else:
        print(f"[run_capability] HARD FAILURE — reason={getattr(result, 'reason', None)}")
        if getattr(result, "operator_note", None):
            print(f"                 operator_note={result.operator_note!r}")


# ---------------- core flows ----------------

def _warn_if_mutating(artifact) -> None:
    """Option D (docs/user_clarifications.md): a mutating capability actually changes target state on every
    replay, and auto-reseed does NOT fire on resource-exhaustion (e.g. insufficient funds) — only on login
    failures, by design. Warn the evaluator up front so they know to re-seed manually if the account runs dry."""
    ctype = getattr(artifact.metadata.capability_type, "value", artifact.metadata.capability_type)
    if ctype != "mutating":
        return
    cap = artifact.metadata.capability_name
    example = "transfers $10 between accounts" if cap == "transfer_funds" else "transfers real funds, updates records"
    print(f"[WARN] {cap} is a mutating capability. Each replay actually\n"
          f"       modifies the target system (e.g. {example}).\n"
          f"       After ~10 replays your seeded account may exhaust its resources.\n"
          f"       \n"
          f"       To refresh: re-run this launcher (it re-seeds automatically when credentials are stale),\n"
          f"       or run: python scripts/seed_parabank_accounts.py")


class PreflightError(RuntimeError):
    """Pre-flight credential verification could not produce LIVE credentials (reseed failed, or a freshly
    seeded account still cannot authenticate — ParaBank down). Carries an actionable message."""


async def _verify_credentials() -> dict:
    """Pre-flight credential verification (Option C) — run ONCE, before any replay/discover attempt, so a
    command never starts with stale credentials.

    Load the JSON registry (seeding on first run), do a lightweight login check, and:
      - LIVE creds        -> return silently (the command proceeds normally);
      - STALE creds       -> reseed a fresh account, VERIFY it authenticates, print a 'refreshed' message,
                             and return the new creds;
      - reseed fails      -> raise PreflightError (ParaBank down / registration failed);
      - fresh account still can't authenticate -> raise PreflightError (ParaBank login endpoint down).
    `login_ok` returns True if the check itself cannot run (e.g. ParaBank unreachable), so a non-auth outage
    is NOT mistaken for a stale account — it propagates through the actual command instead of forcing a
    reseed. No env-var fallback (single JSON source)."""
    creds = await ensure_credentials()
    if await login_ok(creds):
        return creds                                   # fresh — proceed silently
    print("[run_capability] pre-flight: ParaBank credentials are stale (login failed) — refreshing ...")
    try:
        creds = await do_reseed()                      # register a fresh account + rewrite the JSON
    except Exception as exc:  # noqa: BLE001 - registration/network failed
        raise PreflightError(
            f"credential reseed failed (ParaBank may be down): {exc}\n"
            f"  reseed manually with: python scripts/seed_parabank_accounts.py")
    if not await login_ok(creds):                      # brand-new account still can't log in -> ParaBank down
        raise PreflightError(
            "credentials were refreshed but the new account still cannot authenticate — ParaBank's login "
            "endpoint appears to be down; try again in a few minutes")
    print(f"[run_capability] pre-flight: credentials refreshed -> {creds['primary']['username']} "
          f"(checking {creds['primary']['checking_id']})")
    return creds


async def _replay_verified(artifact, *, creds: dict, target_url: str, artifacts_dir: Path,
                           evidence_base: Path, allow_mutating: bool = False) -> object:
    """Replay with the pre-verified LIVE credentials from `_verify_credentials`. No in-flow reseed: the
    pre-flight already guarantees fresh creds, so this path is simple and deterministic. `allow_mutating`
    (D1 fix) is threaded into the shared src.cli.replay_capability boundary, which blocks a mutating
    capability at pre-flight unless the caller passed --i-understand-mutating."""
    cap = artifact.metadata.capability_name
    _warn_if_mutating(artifact)                        # print BEFORE the replay browser starts
    loader = ArtifactStorage(artifacts_dir).load
    url = creds.get("url", target_url)
    params = caller_params_from_sample_invocation(artifact, creds)   # resolves $json: refs from the registry
    return await replay_capability(
        artifact, target_url=url, caller_params=params,
        evidence_dir=evidence_base / f"{cap}_replay_{_ts()}", artifact_loader=loader, interactive=False,
        allow_mutating=allow_mutating)


async def _run(args) -> int:
    target_url = args.target_url or _DEFAULT_URL
    artifacts_dir = Path(args.artifacts_dir)
    evidence_base = Path(args.evidence_dir)
    discover = mode_for(args) == "discover"

    # Fail fast on discover prerequisites BEFORE spending a pre-flight browser.
    if discover:
        if not args.capability_name:
            sys.exit("error: --goal requires --capability-name")
        if not args.capability_type:
            sys.exit("error: --goal requires --capability-type read|mutating (a safety label must be "
                     "declared deliberately; there is no default)")
        try:
            load_api_key()                    # fail fast: discovery needs the LLM
        except AgentConfigError as exc:
            sys.exit(f"error: discovery needs an Anthropic API key — {exc}\n"
                     f"  Uncomment ANTHROPIC_API_KEY in .env (see .env.example) to run discovery.")

    # Pre-flight credential verification (Option C) — ONCE, before ANY replay/discover, for both modes. Loads
    # + seeds the JSON registry and reseeds a fresh account if the current one is stale, verifying it before we
    # commit to the (possibly paid) command. On an unrecoverable credential failure, exit cleanly non-zero.
    try:
        creds = await _verify_credentials()
    except PreflightError as exc:
        sys.exit(f"error: {exc}")

    if discover:
        caller_params = _resolve_caller_params(args.caller_params_from_json)
        caller_sources = _caller_param_sources(args.caller_params_from_json)

        async def _state_fingerprint():
            # Slice 1d: the ParaBank state fingerprint injected into discovery — the account overview
            # (ids + balances), read in its own headless session. Opaque to the agent; a mutating flow
            # mis-declared 'read' moves these balances, tripping the declared-read state-delta refusal.
            return tuple((a["id"], a["type"], a["balance"], a["available"]) for a in await fetch_accounts(creds))

        print(f"[run_capability] discovering '{args.capability_name}' from goal (headed browser) ...")
        dres = await discover_capability(
            goal=args.goal, target_url=target_url, capability_name=args.capability_name,
            capability_type=CapabilityType(args.capability_type),
            caller_params=caller_params, caller_sources=caller_sources, evidence_dir=evidence_base,
            state_fingerprint=_state_fingerprint, interactive=False)
        if dres.artifact is None:
            sys.exit(f"discovery failed: status={dres.status} detail={dres.detail}")
        saved = ArtifactStorage(artifacts_dir).save(dres.artifact)
        print(f"[run_capability] discovered + saved -> {saved} "
              f"(validated={dres.artifact.metadata.validated}) — now replaying it ...")
        artifact = dres.artifact
    else:
        storage = ArtifactStorage(artifacts_dir)
        try:
            artifact = (storage.load_from_path(Path(args.artifact_file)) if args.artifact_file
                        else storage.load(args.artifact_name))
        except ArtifactNotFoundError as exc:
            sys.exit(f"error: {exc}\n\n{list_bundled_artifacts(artifacts_dir)}")

    result = await _replay_verified(artifact, creds=creds, target_url=target_url, artifacts_dir=artifacts_dir,
                                    evidence_base=evidence_base,
                                    allow_mutating=getattr(args, "i_understand_mutating", False))
    _print_result(result)
    return 0 if getattr(result, "status", None) in ("success", "business_outcome") else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python scripts/run_capability.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Evaluator-friendly launcher: replay a bundled capability (no API key), or discover a new "
                    "one from a goal (needs ANTHROPIC_API_KEY) then replay it. Auto-reseeds stale ParaBank "
                    "test accounts.",
        epilog=list_bundled_artifacts())
    p.add_argument("--list-capabilities", action="store_true",
                   help="list bundled capabilities (name, type, description) and exit — no browser")
    p.add_argument("--show-accounts", action="store_true",
                   help="log in and print the ParaBank test accounts (id, type, balance) and exit — "
                        "diagnostic, no capability run; auto-reseeds stale credentials")
    p.add_argument("--artifact-name", help="name of a bundled artifact under artifacts/ (replay mode)")
    p.add_argument("--artifact-file", help="path to an artifact YAML (replay mode)")
    p.add_argument("--i-understand-mutating", action="store_true",
                   help="consent to replay a MUTATING capability (changes real state every run, e.g. "
                        "transfer_funds/request_loan). Without it, a mutating replay is refused (D1 safety gate).")
    p.add_argument("--goal", help="natural-language goal → discover-then-replay mode (requires API key)")
    p.add_argument("--capability-name", help="name for the discovered capability (discover mode)")
    p.add_argument("--capability-type", choices=["read", "mutating"],
                   help="REQUIRED with --goal (discover mode): the caller-declared safety label, 'read' or "
                        "'mutating'. No default — a forgotten label errors rather than guessing.")
    p.add_argument("--caller-params-from-json", nargs="*", metavar="KEY=JSON.PATH", default=[],
                   help="discover mode: map caller params to dot-paths in test_data/parabank_credentials.json "
                        "(e.g. account_id=primary.checking_id)")
    p.add_argument("--target-url", help=f"start URL (default: the JSON registry 'url' or {_DEFAULT_URL})")
    p.add_argument("--artifacts-dir", default=str(_DEFAULT_ARTIFACTS))
    p.add_argument("--evidence-dir", default=str(_DEFAULT_EVIDENCE))
    return p


def main(argv: "list[str] | None" = None) -> int:
    load_dotenv(_ENV_PATH)
    args = build_parser().parse_args(argv)
    if getattr(args, "list_capabilities", False):     # Fix 1: list + exit, no browser, no credentials
        print(list_bundled_artifacts(Path(args.artifacts_dir)))
        return 0
    if getattr(args, "show_accounts", False):          # diagnostic: log in + print account state, then exit
        return asyncio.run(_show_accounts(args))
    if mode_for(args) == "replay" and not (args.artifact_name or args.artifact_file):
        sys.exit("error: replay mode needs --artifact-name or --artifact-file "
                 "(or pass --goal to discover a new capability).\n\n" + list_bundled_artifacts())
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
