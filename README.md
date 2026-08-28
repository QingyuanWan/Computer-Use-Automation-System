# interfaceai

**Record once, replay many** — let an AI agent operate legacy back-office systems that expose **no API**. An LLM
explores a goal once against the live UI (discovery) and records it as a typed, replayable artifact; production
then replays that artifact deterministically, with no LLM in the loop. Design rationale is in
**[REPORT.md](REPORT.md)**; the demo target is [ParaBank](https://parabank.parasoft.com/parabank), a
server-rendered banking sandbox.

## Prerequisites

- **Python 3.10+** (developed and tested on 3.12.5).
- Install:
  ```bash
  git clone <repo-url> && cd interfaceai      # <repo-url> is a PLACEHOLDER — replace at publish
  python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
  pip install -r requirements.txt && playwright install chromium
  cp .env.example .env                                   # PowerShell: Copy-Item .env.example .env
  ```
- **A headed Chromium window opens on every command** — replay and discovery both drive a *visible* browser. A
  display is required; there is no headless mode.
- **Network:** every command reaches ParaBank; only discovery (below) reaches the Anthropic API. Replay needs no
  API key — leave `.env` empty unless you run discovery.

## Keyless walkthrough — no API key required

Everything in this section runs without an Anthropic key. Test accounts are seeded automatically into a
gitignored registry (`test_data/parabank_credentials.json`); you never edit them. **ParaBank purges seeded
accounts every 30–60 minutes** — `run_capability.py` runs a login pre-flight and auto-reseeds when they go stale
(the lower-level `python -m src.cli` does *not*, which is why the launcher steps below keep the raw-CLI ones on
fresh credentials).

**Step 1 — see the accounts (this also seeds the registry).**
```bash
python scripts/run_capability.py --show-accounts
```
A Chromium window opens, the launcher seeds/refreshes the accounts (you may see a `pre-flight: credentials
refreshed` line), and it prints the checking / savings / loan balances. Run this first — it primes the registry
so every later command has valid credentials.

**Step 2 — deterministic replay of a read capability.**
```bash
python scripts/run_capability.py --artifact-name lookup_checking_balance
```
Logs in and reads the checking account. Expected: `SUCCESS — outputs={'account_type': 'CHECKING', 'balance':
'$…', 'available': '$…'}`, exit 0. No LLM is called.

**Step 3 — the safety gate on a mutating capability.** `transfer_funds` moves real money, so it is refused
without explicit consent:
```bash
python scripts/run_capability.py --artifact-name transfer_funds
```
Expected: `REFUSED … Re-run with --i-understand-mutating`, exit 1. To actually move $10 checking→savings:
```bash
python scripts/run_capability.py --artifact-name transfer_funds --i-understand-mutating
```
Expected: `SUCCESS`, exit 0 — confirm with `--show-accounts`. **Mutating replays change real state every run;
after ~10 the account exhausts (a loan gets denied) — rerun `--show-accounts` to reseed.**

**Step 4 — human-in-the-loop (you drive the browser).** `human_input_demo` pauses for a person. It must be the
**raw CLI** (the launcher is non-interactive and won't wait), and because a `human_input` step is treated as
mutating it needs consent:
```bash
python -m src.cli replay --artifact-name human_input_demo \
    --caller-params-from-json account_id=primary.checking_id --i-understand-mutating
```
A Chromium window opens on the ParaBank login page with an in-browser panel (a single **Done** button). **Type
both the username and password** into the login form — the panel prompt mentions only the username, but login
needs both fields. The password is generated randomly at seed time, so read both current values from the
gitignored registry:
```bash
python -c "import json;d=json.load(open('test_data/parabank_credentials.json'))['primary'];print('user:',d['username']);print('pass:',d['password'])"
```
**Do not click Log In** — click **Done**. The system then clicks Log In, navigates, and reads the account; expected
terminal: `status=success`.

**Step 5 — a business outcome (a result, not a crash).** The captured contract ships in
`evidence/lookup_checking_balance_replay_20260827_051820/` — a system-emitted `cli_summary.json`, verbatim
`stdout.txt`, and a masked screenshot showing `status=business_outcome`, `outcome_name=account_not_found`, and
**exit 0**. A missing account is a legitimate result, not a crash — so it exits 0, not non-zero; that
distinction, business outcome versus failure, is the design mistake the brief names most often, and it is
provable from that evidence regardless of the target's live state.

Reproducing it live is optional and target-dependent, and the caveat matters *before* you run it: ParaBank
intermittently returns a generic 500 for a missing account instead of the clean "Could not find account" text,
and when it does replay fails closed and escalates rather than mislabeling an unknown state — **click Abort or
press Ctrl-C**. That is the documented drift (REPORT §Determinism), not a setup problem. The command is raw-CLI
(it passes an invalid id directly, which the launcher can't) and runs on the credentials step 1 seeded:
```bash
python -m src.cli replay --artifact-name lookup_checking_balance \
    --caller-params-from-json username=primary.username password=primary.password account_id=invalid_account_id
```
When ParaBank serves the not-found text, expected: `status=business_outcome` / `account_not_found` / exit 0. (If
it's been more than ~30 minutes since step 1, re-run step 1 first — this command is raw-CLI and does not
auto-reseed a purged account.)

## Discovery — the one part that needs an Anthropic API key

Put your key in `.env` (`ANTHROPIC_API_KEY=…`; get one at <https://console.anthropic.com/>). Discovery drives
ParaBank live to author a new artifact, then replays it to validate.

```bash
python scripts/run_capability.py --goal "Look up the checking balance" \
    --capability-name my_lookup_probe --capability-type read \
    --caller-params-from-json username=primary.username password=primary.password account_id=primary.checking_id
```
All three caller-params are required (the LLM must authenticate), and `--capability-type` is required with
`--goal` (a `read`/`mutating` safety label, no default). It takes **~1–3 minutes and roughly $0.20**, and a
`read` goal does not change account state. It saves `artifacts/my_lookup_probe.yaml` — a throwaway that will then
show up in `--list-capabilities`; delete it when you're done — and replays it.

**A fresh discovery may print `validated=False` and then a hard failure. This is not a broken setup.** Success
checkpoints are authored from the phrases the model cites, and a particular phrasing can produce a checkpoint
that cannot match on replay (checkpoint-phrasing variance, REPORT §Determinism); re-running usually validates.
The shipped `lookup_checking_balance` is the hand-verified version of exactly this capability.

*Windows PowerShell:* `$` inside a double-quoted `--goal` is read as a variable, so use single quotes for goals
that contain amounts — `--goal 'Transfer $10 from checking to savings'` — or escape each `$` with a backtick.

## Command reference

All via `python scripts/run_capability.py` (the raw path is `python -m src.cli {discover,replay} …`):

| Flag | Description |
|------|-------------|
| `--artifact-name NAME` | Replay a bundled capability (no API key) |
| `--artifact-file PATH` | Replay a capability from a YAML file |
| `--goal "…" --capability-name NAME --capability-type read\|mutating` | Discover from a goal, then replay (needs API key). `--capability-type` is **required** with `--goal` — no default. |
| `--caller-params-from-json KEY=JSON.PATH` | Bind caller params to dot-paths in the registry (e.g. `account_id=primary.checking_id`) |
| `--i-understand-mutating` | Consent to replay a **mutating** capability (changes real state); required for `transfer_funds` / `request_loan` / `human_input_demo` |
| `--list-capabilities` | List bundled capabilities and exit |
| `--show-accounts` | Log in and print the ParaBank test accounts |
| `--target-url URL` | Override the start URL (default: ParaBank) |

**Bundled capabilities:** `lookup_checking_balance` (read), `transfer_funds` (mutating — $10 checking→savings),
`request_loan` (mutating — $500 loan, returns the new loan account number), `human_input_demo` (mutating —
pauses for a person).

Operational detail — ParaBank quirks, reseeding, credential registry — is in
**[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**. Design is in **[REPORT.md](REPORT.md)**. Released under the MIT
License.
