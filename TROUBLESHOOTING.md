# Troubleshooting

Operational edge cases for running the demo against the live ParaBank sandbox. Design rationale lives in
[REPORT.md](REPORT.md).

## Credentials & ParaBank test accounts

- **`.env` holds only `ANTHROPIC_API_KEY`** (needed for discovery, not replay). Copy it from `.env.example`.
- **ParaBank test accounts are auto-managed** in `test_data/parabank_credentials.json` (gitignored). The
  launcher seeds them on first run; `python scripts/seed_parabank_accounts.py` (re)seeds manually.
- **`--caller-params-from-json KEY=JSON.PATH`** binds caller parameters to dot-paths in that JSON (e.g.
  `account_id=primary.checking_id`). It is the only credential source — there is no env-var path (the old
  `$env:`/`--caller-params-from-env` bridge was removed). A missing file or an absent dot-path fails fast with
  a message pointing at `python scripts/seed_parabank_accounts.py`.
- **ParaBank purges accounts every ~30–60 min.** `run_capability.py` runs a login pre-flight before each
  replay and **auto-reseeds** if the accounts no longer authenticate. The lower-level `python -m src.cli`
  does **not** auto-reseed — re-run the seed script or use `run_capability.py`.
- **`--show-accounts`** logs in and prints current balances — handy to confirm a mutating capability
  actually changed state (e.g. checking dropped $10 after `transfer_funds`).

## Mutating capabilities

`transfer_funds` and `request_loan` are **mutating**: each replay changes real state (transfer_funds moves a
real $10 checking→savings; request_loan opens a loan). A mutating replay is **refused by default** — pass
`--i-understand-mutating` to consent (the runtime safety gate blocks it otherwise, on both
`run_capability.py` and `python -m src.cli replay`; `run_capability.py` also prints a `[WARN]` describing the
effect). After ~10 transfers a freshly-seeded checking account may run low. Auto-reseed triggers only on
login/stale-credential failures, **not** on resource exhaustion (insufficient funds is a legitimate business
outcome, surfaced rather than hidden). Verify a transfer with `--show-accounts` or by replaying
`lookup_checking_balance`.

## Runtime

- **A browser window opens** — by design (always headed so a human can take over on escalation). A headless
  server/CI box **without a display** cannot run replay/discovery.
- **`error: discovery needs an Anthropic API key`** — a `--goal` (discovery) command without
  `ANTHROPIC_API_KEY` in `.env`. Replay (`--artifact-name`) needs no key.
- **Seed script hangs / `seeding failed`** — ParaBank may be slow or briefly down; wait a minute and retry.
- **See the escalation/takeover panel** — replay the bundled fixture (it force-fails a checkpoint):
  ```
  python -m src.cli replay --artifact-file test_artifacts/phaseB_escalation_test.yaml \
      --caller-params-from-json username=primary.username password=primary.password \
          account_id=primary.checking_id
  ```

## Platform notes

- **Windows (PowerShell):** double-quote `--goal "..."`; activate with `.venv\Scripts\Activate.ps1`; use
  `Copy-Item .env.example .env` instead of `cp`.
- **Linux:** if `playwright install chromium` reports missing system libraries, run `playwright install-deps`.
