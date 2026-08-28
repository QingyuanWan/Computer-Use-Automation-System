# finding v — the checkpoint-phrasing flap, as a controlled A/B

Two artifacts discovered by the **same pipeline** from the **same goal** ("look up the checking balance"),
against the **same account** (checking `108360`, balance `$350.50`), **17 minutes apart** on the same machine.
Files renamed here for a reviewer; internal `capability_name` is `manual_read` / `manual_read2` (verbatim pipeline
output, zero hand-edits).

| Artifact | success checkpoint phrases | replay |
|----------|---------------------------|--------|
| `finding_v_fail.yaml` (`manual_read`)  | `'Account Type: {{account_type}}'`, `'Balance: {{balance}}'` — value **concatenated** into the label | **hard_failure / stub_unavailable**, `outputs={}` |
| `finding_v_pass.yaml` (`manual_read2`) | `'Account Type:'`, `'{{account_type}}'`, `'Balance:'`, `'{{balance}}'` — label and value as **separate** phrases | **success**, `outputs={account_type: CHECKING, balance: $350.50}` |

`checkpoint_phrasing.diff` is the full unified diff. The **only material difference is the checkpoint phrasing**;
every other change is cosmetic (`capability_name`, an LLM word swap in `escalation_hint`) or the outcome
(`validated` false→true). Steps and captures are byte-identical.

`fail_replay_screenshot.png` is the failure screenshot: the page is **fully rendered** — Account Number `108360`,
Account Type: `CHECKING`, Balance: `$350.50` — so **every required phrase's content is present**, yet the
checkpoint timed out. That rules out data, timing, and target drift. The cause is that the DOM renders the label
and value in separate cells, so `inner_text` joins them with a tab/newline, and the concatenated
`'Account Type: CHECKING'` (a literal space) is not a substring of `'Account Type:\tCHECKING'`. The separated
phrases in the passing artifact each match independently.

`pass_replay_cli_summary.json` is the passing run's system-emitted result. See REPORT §Determinism (finding v).
