# REPORT

A computer-use automation system for interface.ai's take-home: it lets an AI agent operate legacy back-office web
apps with no API, on a record-once/replay-many model — an LLM explores a goal once against the live surface
(discovery), the successful path compiles into a typed serialized artifact, and production replays it
deterministically with no LLM in the loop.
The reference target is ParaBank (parabank.parasoft.com), a server-rendered banking sandbox with inconsistent
element ids, AJAX-populated tables, and same-URL success states.

## Architecture

A single Python process, no runtime services — deliberately, against §7's caution on scaling infrastructure
(ADR-008); the demo path is two shell commands or the `run_capability.py` launcher.

The load-bearing decision is separating discovery from replay (ADR-005/006). Both the goal *and* the entry-point
URL are caller inputs to `discover` (`--goal`, `--target-url`), so nothing about the target is compiled in.
Discovery is an LLM observe→decide→act loop perceiving each page through the accessibility tree
(`page.locator("body").aria_snapshot()`), not the raw DOM — a screenshot is a fallback only — so the approach
carries to surfaces with no clean DOM (§3.1). Emission compiles the recorded steps into a typed artifact:
reverse-parameterizing caller values into `{{templates}}`, authoring checkpoints from the model's cited success
phrases, and running a failure-injection pass for business outcomes. This genuine LLM discovery run is the part
that cannot be mocked (`evidence/discovery_request_loan_*/`). Replay executes each step against a fresh browser
session with zero LLM calls; the halves communicate only through the artifact.

The discovery loop is bounded by max steps, repeated-action failures, an explicit `finish`, and the model
ceasing to call tools — but has no wall-clock bound, so a hung model call, navigation, or browser wait is
uncovered; a wall-clock timeout is missing. The one seam that names Playwright is `src/executor/`, the
surface-abstraction boundary of §4.

## Artifact schema

A capability is one Pydantic-typed, versioned YAML document — `version` / `metadata` / `parameters` / `captures`
/ `steps`; the worked example is `artifacts/lookup_checking_balance.yaml`.

**A typed artifact, not a recorded script.** The naïve alternative emits a parameterized Playwright script, which
fails three ways. A script is opaque procedure — a calling agent cannot learn its inputs or outputs without
running it — whereas the artifact is a contract: `parameters` is the typed signature and the `export: true`
captures are the typed return shape, both introspectable before execution (the basis for the §8 agent-facing
interface). A script welds the capability to one runtime, whereas the artifact stores intent (semantic locators,
not API calls) behind the Executor seam (§4). And it is diffable: declarative YAML lets emission guards (§6) and
a reviewer confirm statically that no credential or session-specific id was baked in.

**One `{{name}}` namespace, with two weak invariants.** Any `{{name}}` resolves against parameters ∪ captures —
an account id flows from a parameter into a URL and checkpoint phrases; a capture is read at one step and
asserted at another — failing loudly on an undefined name. But on a name collision the code resolves captures
before parameters, so a capture silently shadows a same-named parameter; and nothing forbids a checkpoint from
referencing a capture its own step binds (the engine binds, then checks) — the self-referential checkpoint behind
finding v (§Determinism). The right rules — a collision error, and checkpoints restricted to earlier-bound
captures — are not enforced today.

**Other keys.** `export: true` (optionally `returned_as`) joins a capture to the caller-facing return set — no
separate `outputs` block. Locators are semantic (role + name, href/id anchors, an ordered `fallbacks` chain
refusing an ambiguous match), though the pipeline emits single-strategy locators only, and ParaBank's unlabeled
login inputs resolve by positional `role_nth`. `sample_invocation` holds a `$json:` registry reference per
parameter, so the artifact stores a reference, never a secret.

## Determinism & error handling

Replay is decision-deterministic — no LLM, no sampling, no run-to-run variation (ADR-005) — though it drives a
live AJAX-rendered site whose timing varies, absorbed by bounded polling (5 s).

Outcomes are a three-way contract: `success` returns the exported captures; `business_outcome` is a legitimate
non-crash answer ("no such account" is a result) when a checkpoint's `expected_outcomes` match; `hard_failure`
carries one of four subtypes — `stub_unavailable`, `human_aborted`, `escalation_exhausted`, `technical_error` (a
fifth "recoverable" designed, unbuilt). These four are not one taxonomy — `technical_error` is a failure cause,
`stub_unavailable` an operator condition, `human_aborted` a decision, `escalation_exhausted` a disposition — and
`safety_blocked` (§6) alongside them conflates the axes further; the cleaner shape is orthogonal (failure class ×
recoverability × escalation disposition), the current flat enum a pragmatic shortcut. Every `hard_failure`
carries the failing step id, expected phrases, observed text, and a screenshot (ADR-004).

A `hard_failure` carries **no outputs**, by design: captures bound during a failed run are unverified. Observed
in the submitted evidence, a pipeline artifact bound `new_loan_account_number` to the funding account, not the
new loan (`evidence/my_loan_replay_20260827_165252/`). *Never return unverified data* and *never lose the result
of an irreversible action* pull against each other; today the system chooses the first, and the evidence
screenshot — holding the value the capture missed — is the only bridge back. The corrected capture does
reconcile: on a live `request_loan` its exported value matched real account state via `--show-accounts`
(`evidence/request_loan_replay_20260827_224901/`).

The deeper production question: when an action was submitted and its result is unconfirmed, did it commit? The
design answer (forward-plan item 3): mark explicit commit boundaries in the artifact and never blind-retry across
one; use a target idempotency key where the target supports one, and independently persist a caller invocation
id to deduplicate repeated *caller* requests — but a local invocation id cannot make a target de-duplicate a
commit it already accepted, so after an ambiguous commit the actual safety mechanism is to read back and
reconcile target state (does the loan or transfer now exist?) before retrying, and to escalate if the commit
cannot be determined.

Replaying `lookup_checking_balance` with a missing `account_id` returns `business_outcome` / `account_not_found`
/ `reason=null`, exit 0 (`evidence/lookup_checking_balance_replay_20260827_051820/`) — the brief's most-cited
mistake, handled. Every terminal outcome leaves a system-emitted `cli_summary.json` plus a masked non-failure
screenshot (§3.5). Of §3.3's six conditions, three are handled (record-not-found, dialog, slow load),
validation-error collapses into not-found here, permission-denial would be an `expected_outcomes` branch
(ParaBank declares none), and session-timeout is recognized but unrecovered (forward-plan item 4). A step's
`expected_outcomes` take precedence over a capture failure and are declared on the step where the condition
manifests — the ADR-005 ordering that makes "no such account" a `business_outcome` rather than a
`technical_error`. When a declared branch does not match — ParaBank flip-flops between the not-found text and a
generic 500 — replay fails closed rather than recategorizing an unrecognized state.

The checkpoint matcher has a hard expressiveness boundary: `text_all_present` (presence-only substring AND)
cannot express an *empty* result — a no-rows page is a text-subset of the populated one, so the signal is the
*absence* of rows, the most common search outcome, inexpressible. The fix is a different assertion language, not
more source-recording: a small typed predicate set — `element_exists`/`element_absent`,
`text_equals`/`contains`/`matches`, `row_count(op, n)`, `capture_equals`, `url_matches`, composed with
`AND`/`OR`/`NOT` — makes an empty result expressible and success a declared predicate, not whatever prose the
model cited (forward-plan item 2).

That dependence is real: emission sometimes concatenates a captured value into a success phrase the DOM
whitespace makes unmatchable, so two runs of the same goal produced `validated=False` and `validated=True` on
this alone (a controlled A/B in `evidence/finding_v_checkpoint_flap/`); worse on a mutating flow, `my_loan`
templated the generated `{{new_loan_account_number}}` into its own success condition and `css:body` re-bound the
wrong value, so the loan was created but the checkpoint could not match. Mutating capabilities ship
`validated=false` for an independent reason — the validation replay is the mutation — so three of four bundled
are unvalidated; discovery still exercises the action once against real accounts, which production authoring
could confine to an isolated tenant, a resettable fixture, or a dedicated test record, none built here.

## Heterogeneity & multi-tenant

Exercised against one target; the direct answer is the cross-tenant result. `lookup_checking_balance`,
discovered against one tenant, replayed unmodified against a second freshly-seeded tenant and returned `success`
(`evidence/phaseB_ci_run_test4_*/`) — only the credential registry changed. The mechanism is schema-level (§3.7):
registry-referenced `sample_invocation` makes every tenant value a `{{template}}`, so onboarding a tenant is a
pure data operation.

The loop is surface-agnostic *in kind*: discovery perceives the page through the accessibility tree
(role/name/value, not HTML), and desktop apps expose the same class of tree via UIAutomation / AX API / AT-SPI.
What is web-specific is one adapter — Playwright — behind the Executor, whose interface is four methods a second
backend implements (designed, not delivered): `resolve_locator`, `execute_action`, `resolve_checkpoint`,
`start`/`stop`. The schema splits along that seam — `parameters`, `captures`, `expected_outcomes`, checkpoints,
`human_input` are surface-independent; only the locator strategy enum is surface-specific. So legacy web and
desktop are one porting exercise: on legacy web the role/name strategies degrade to positional `role_nth` and the
enum needs a frame/scope field; on desktop only the Executor and the enum are new.

Per-tenant *UI* variance is the unsolved half: a tenant branding "Log In" as "Sign On" breaks a semantic
locator. The design answer is a per-tenant override layer — one base artifact plus a thin patch over the
differing locator fields — leaving steps, captures, and checkpoints shared; drift is detectable LLM-free (a
rising `locator_exhausted` rate, a scheduled canary replay). The hooks exist; the automation is deferred (§7).

## Escalation & handoff

The system implements a real pause–handoff–resume against the same live session (§3.6); only the console's
richness is scoped down. One injected overlay panel (`src/escalation/`, via `expose_binding` and an
`asyncio.Event`) serves reactive escalation on a stuck condition — Resume / Take over & resume / Abort — and
planned escalation at a `human_input` step (single Done); a safety violation is terminal `safety_blocked`, never
escalated. Discovery records each resolved intervention as a `human_input` step, so a flow that needed a human
once re-triggers that pause on replay (ADR-007). Take-over is two-phase — the human acts in planned mode, then on
Done the engine re-observes and re-evaluates the checkpoint once; every wait is bounded (unattended →
`stub_unavailable`). Four escalation scenarios are verified live (`evidence/phaseB_ci_run_test*/`).

Recording what the human did is the largest §3.6 gap: today privacy is achieved by observing nothing during
takeover, which also records nothing beyond boundary metadata (step, reason, outcome, duration) and an optional
note — action capture is not implemented. The stronger design is selective, redacted observation: log semantic
actions (control clicked, navigation, field typed into) with sensitive values `[REDACTED]`, tie them to an
intervention id, and require a structured operator summary — which cannot stay optional if it is the only record.

Underneath the panel is a control-ownership model, partly enforced and partly design. The intended states are
`AUTOMATION_OWNED → PAUSED → HUMAN_OWNED → VERIFYING → AUTOMATION_OWNED` (or a terminal). What the code enforces
today: exactly one controller acts, because the replay coroutine blocks on the escalation `Future` while the
human owns the session (single-threaded — no dispatch can race the human); observation is suspended while a
boolean `is_takeover_active` is set; and `Done` does not merely resume — the engine re-observes and re-evaluates
the checkpoint once (the `VERIFYING` step) before proceeding. Timeout and abort settle to defined terminal
outcomes. What is design, not delivered: the states are a two-value flag plus control-flow, not a first-class
machine with an enforced ownership guard — "one controller" and "no dispatch while the human owns" hold by
construction (blocking await), not by a check that would reject a violating dispatch.

## Safety

The persistence invariant is precise: never serialize a secret into a capability artifact, a log, or evidence.
(Secrets do live on disk — an API key in `.env`, test credentials in a gitignored registry — but never inside an
artifact or its evidence.) The second threat: never let the discovering LLM or a replay step act off the
sanctioned surface.

Emission mandates credential parameterization (ADR-010): credential-named fields auto-parameterize to
`{{username}}`/`{{password}}` (`sensitive: true`), and a value-shape net refuses to emit if a credential-shaped
literal survives in an unnamed field. It deliberately over-refuses — a synthesized address like `123 Main St`
trips it, so a full register-then-read flow cannot be discovered end-to-end (fail loud rather than emit a
misclassified field).

A `SafetyGate` (ADR-012) enforces, before every dispatch, a domain/action-type allowlist (off-allowlist →
terminal `safety_blocked`), and at replay pre-flight a mutating-consent gate (a `mutating` capability is refused
without `--i-understand-mutating`). `capability_type` is caller-declared, not inferred, and enforced three ways —
`human_input` forces `mutating`; the validator refuses a declared `read` carrying one; and a discovery-time
state-delta probe refuses to emit a declared `read` that changed an opaque account-overview fingerprint (it
detects what the run DID, not what it COULD do, and runs after the body, so it blocks shipping, not the mutation
itself).

Redaction is declaration-based: `PIIRedactor` redacts `sensitive` values in discovery text, and masking blacks
out `sensitive`-bound inputs in replay screenshots — covering artifact-supplied credentials, but not those a
human types during escalation (protected instead by suspending observation; the residual is a screenshot taken
while the login form is filled) nor the business-data captures (`css:body`; forward-plan item 1). Only discovery
calls the Anthropic API.

## Cuts

Still not built (each disclosed inline where it lives):

- **Capture provenance** — `css:body` extraction is not element-scoped: `balance`/`available` collapse, `request_loan` returned a wrong number until hand-corrected, and a late-rendered element isn't polled (forward-plan item 1).
- **Checkpoint expressiveness** — no empty-result predicate (forward-plan item 2); a malformed and a missing id are indistinguishable here.
- **Regulated data in screenshots** — business-data captures are unmasked; a throwaway username survives text redaction; OCR deferred.
- **Non-idempotency** — mutating replays change real state and commit before the checkpoint, so a failure cannot prevent the side effect; fixture/rollback deferred.
- **Per-tenant isolation** — credential registry and allowlist are single flat files, unkeyed (forward-plan item 5).
- **Worked-example hand-edits** — four (YAML comments; `evidence/pipeline_output_unedited_20260827.yaml`), two load-bearing: the checkpoint's stable labels and `request_loan`'s label-anchored capture. A separate hand-edit corrects `human_input_demo`'s operator prompt to name both login fields — operator-facing text, not replay behaviour, so it is disclosed in that artifact and not counted among these four.
- **Escalation edges** — `confirm`/`prompt` unhandled; a demo-tuned 60 s human-input timeout; full page observations sent to the Anthropic API.

**What I'd build next** — the core artifact/replay contract before operability at scale, which §7 deprioritizes:

1. **Capture provenance** — source-element recording so emission emits a cell/link locator, not `css:body`; the captures are the return contract and one was wrong, and the same signal unblocks business-data redaction.
2. **Checkpoint predicate language + approval stability** — the predicate set (§Determinism), an N-run stability signal, a `draft → approved` gate.
3. **Ambiguous-commit / idempotency** (§Determinism) — the deepest banking gap, observed with `my_loan`.
4. **Session-expiry recovery** — recognized, unbuilt; below the correctness items.
5. **Multi-app/tenant registry** — keyed policy and credentials per target; operability at scale, deliberately last.
