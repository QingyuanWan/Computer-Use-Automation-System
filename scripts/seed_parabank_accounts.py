"""One-shot ParaBank test-data seeding for the ADR-9 validation registry.

NO LLM — this is deterministic Playwright provisioning code, not a capability. Run it to (re)provision the
ParaBank test accounts used by replay/validation:

    python scripts/seed_parabank_accounts.py

It registers a fresh PRIMARY user (a new random-suffixed username each run — ParaBank rejects duplicate
usernames, so reuse is not attempted) and opens one savings account, then writes the credentials to
`test_data/parabank_credentials.json` (gitignored — see scripts/credentials.py). ParaBank persists accounts
across sessions but resets periodically; `run_capability.py` re-seeds automatically when they go stale.

These are TEST users on a public sandbox (a proxy target) — never real credentials or PII (§4/§3.4).
"""
from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "https://parabank.parasoft.com/parabank"      # single-source: no env-var configuration

_REGISTER_FIELDS = {
    "customer.firstName": "Ada",
    "customer.lastName": "Registry",
    "customer.address.street": "1 Registry Way",
    "customer.address.city": "Testville",
    "customer.address.state": "CA",
    "customer.address.zipCode": "94000",
    "customer.phoneNumber": "5550100100",
    "customer.ssn": "111-22-3333",
}


async def _seed() -> dict[str, str]:
    # ParaBank silently truncates usernames past a length limit (empirically ~15-20 chars) and then rejects
    # the truncated value as "already exists". Keep the username short: "itfai_" (6) + 8 hex = 14 chars.
    username = f"itfai_{secrets.token_hex(4)}"
    # Generate a fresh password per seed and NEVER hardcode it: the value must live only in the gitignored
    # credentials file, so a committed literal cannot contradict the .gitignore that exists to keep it out of
    # the repo. Constraints: letters+digits only, no punctuation. ROOT CAUSE (verified 2026-08-25): ParaBank
    # ACCEPTS a '#' at registration but its form-login endpoint (login.htm POST) returns HTTP 500 when the
    # password contains '#' — an account with '#' could register yet never form-login, which the
    # pre-flight/replay misread as "ParaBank down". The "Pw1" prefix guarantees the value stays
    # credential-SHAPED (len>=6, mixed letters+digits) so the ADR-010 B1 detector still treats it as a
    # credential; token_hex uses the same secrets-based generator shape as the username above.
    password = "Pw1" + secrets.token_hex(8)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            # 1) register (ParaBank auto-logs-in on success)
            await page.goto(f"{BASE}/register.htm")
            for fid, val in _REGISTER_FIELDS.items():
                await page.fill(f'[id="{fid}"]', val)
            await page.fill('[id="customer.username"]', username)
            await page.fill('[id="customer.password"]', password)
            await page.fill('[id="repeatedPassword"]', password)
            await page.click('input[value="Register"]')
            await page.wait_for_load_state("networkidle")
            body = (await page.locator("body").inner_text()).lower()
            if "created successfully" not in body and "welcome" not in body:
                # surface ParaBank's own field-validation error(s) so failures are self-diagnosing
                errors = await page.locator("span.error").all_inner_texts()
                detail = f"validation errors: {errors}" if errors else f"page head: {body[:200]!r}"
                raise RuntimeError(f"registration did not complete; {detail}")

            # 2) read the pre-created checking account id from the overview table
            await page.goto(f"{BASE}/overview.htm")
            await page.wait_for_selector("#accountTable a", timeout=15000)
            checking_id = (await page.locator("#accountTable a").first.inner_text()).strip()

            # 3) open a savings account funded from checking
            await page.goto(f"{BASE}/openaccount.htm")
            await page.wait_for_selector("#type", timeout=15000)
            await page.select_option("#type", label="SAVINGS")
            # <option>s are never "visible" to Playwright; wait for the AJAX-populated option to be ATTACHED.
            await page.wait_for_selector("#fromAccountId option", state="attached", timeout=15000)
            await page.click('input[value="Open New Account"]')
            await page.wait_for_selector("#newAccountId", timeout=15000)
            savings_id = (await page.locator("#newAccountId").inner_text()).strip()
        finally:
            await browser.close()

    return {
        "PARABANK_PRIMARY_USERNAME": username,
        "PARABANK_PRIMARY_PASSWORD": password,
        "PARABANK_PRIMARY_CHECKING_ID": checking_id,
        "PARABANK_PRIMARY_SAVINGS_ID": savings_id,
    }


async def _main() -> int:
    try:
        env = await _seed()
    except Exception as exc:  # noqa: BLE001 - surface a clear message; ParaBank can be flaky/reset
        print(f"ERROR: seeding failed: {exc}", file=sys.stderr)
        return 1
    # Write JSON credential storage (test_data/parabank_credentials.json), not .env (docs/setup_bundle_v2).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.credentials import from_seed_dict, save_credentials
    creds = from_seed_dict(env)
    path = save_credentials(creds)
    print(f"Seeded fresh ParaBank test accounts -> {path}")
    print(f"  primary user: {creds['primary']['username']}  "
          f"checking: {creds['primary']['checking_id']}  savings: {creds['primary']['savings_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
