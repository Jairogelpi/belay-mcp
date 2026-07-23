# Belay — Plan v2 (post-v0.1.0): E10-E15

Continuation of `docs/plan.md`'s methodology (SDD+TDD, one PR/commit per
entrega, English artifacts, no LLM anywhere in `belay/`, no `eval`/`exec`).
Entregas are additive — they must not weaken any prior test or break L3
conformance (`belay-conformance run --target belay --level 3` must still
pass after each).

## E10 — Statistical anomaly baselines (DONE, shipped)
## E11 — Real SQL dry-run adapter (DONE, shipped)
## E12 — Counterfactual replay (DONE, shipped)
## E13 — Cryptographically signed, offline-verifiable evidence (DONE, shipped)

See `docs/adr/0010`-`0013` for each.

## E14 — Identity attribution: who told the agent to do this

**Problem:** the ledger already records `approved_by` (who authorized a
paused action, §7/§12) but nothing records *who launched the agent
session in the first place* or *on whose behalf* an action was taken. In
an org with many employees and many agents sharing one Belay deployment,
an audit today can answer "what happened and who approved the risky
step" but not "which human's instruction produced this session at all" —
the actual accountability chain enterprises ask for when rolling out
agentic tools to a workforce.

**Constraint:** no new trust mechanism invented — Belay does not
authenticate humans itself (out of scope, spec has no auth layer); this
entrega records and enforces the presence of an **externally-asserted**
identity (an API key label, an SSO-issued claim string, a service
account name — whatever the deployment's own auth in front of Belay
already established) as a first-class, immutable, signable field. Do not
build a login system. Reuse E13's signing so identity claims become part
of the tamper-evident evidence, not a bolt-on.

**Design:**
- `belay/ledger/model.py`: add `initiated_by: str | None` (the human/service
  identity that started the session) — additive field on `Event`, no
  schema break (the model already has `extra="allow"`, but promote this
  specific field to a named, typed, documented one since it's now
  load-bearing, not incidental metadata).
- `belay/proxy/lifecycle.py`: `Lifecycle`/`start_session()` takes a
  required `initiated_by: str` parameter (no silent default — an
  unattributed session should be a deliberate, explicit choice like
  `"unknown"`/`"anonymous"`, never accidentally blank) and stamps it onto
  `session_started` and every subsequent event for that session (either
  by repeating it per-event or by treating `session_started`'s value as
  binding for the whole session and letting `belay/ledger/replay.py`'s
  `SessionState` surface it — pick whichever avoids redundant storage,
  document the choice).
- `belay/approvals/queue.py`: the existing `approved_by` stays exactly as
  is (§12 already covers it) — E14 is only additive on the *initiator*
  side, do not touch approval semantics.
- On-behalf-of chains: if an agent session was itself launched by another
  automated process rather than directly by a human (e.g. a scheduler
  service acting for a named employee), support an optional
  `on_behalf_of: str | None` alongside `initiated_by` (the calling
  identity vs. the accountable human) — both flow into the same event
  stamping mechanism.
- CLI: `belay wrap`/`belay run` gain a `--initiated-by <identity>`
  (required, or defaulted to an explicit loud `"unknown"` string if
  omitted — never silently empty) and optional `--on-behalf-of
  <identity>`. `belay verify`/`belay verify-evidence` (E13) output must
  surface `initiated_by`/`on_behalf_of` in their report so an auditor
  sees it without a manual ledger query.
- E13 integration: `sign_session`'s signed summary (currently session_id,
  set_hash, chain_head_hash, event_count, signed_at) gains `initiated_by`
  (and `on_behalf_of` if present) so identity attribution is itself
  covered by the cryptographic signature — tampering with who initiated a
  session must be detected exactly like tampering with the chain.

**Tests (TDD, red before green):**
- `start_session` without `initiated_by` is a type/call error (or an
  explicit loud default), never silently blank — write the test that
  proves an accidental omission is caught, not swallowed.
- `initiated_by`/`on_behalf_of` appear on `session_started` and are
  retrievable via `replay()`'s `SessionState` for the whole session.
- `belay verify-evidence` (E13) reports `initiated_by` and detects
  tampering with it exactly like any other summary field (reuse E13's
  existing tamper-detection test pattern — this is a regression-style
  test proving the two entregas compose correctly, not a new mechanism).
- CLI: `belay wrap ... --initiated-by alice@corp` then `belay run` then a
  real stdio call produces a ledger where `belay verify-evidence` surfaces
  `alice@corp` as initiator.
- Multiple sessions from different initiators against the same wrapped
  server never cross-contaminate (each session's events carry only its
  own initiator) — property or parametrized test over N sessions.
- Regression: all E0-E13 tests still pass; sessions started without
  identity attribution via any pre-E14 test helper either get updated to
  pass an explicit identity or get an explicit `"unknown"` default — no
  test should be silently broken by the new required parameter.

**Exit:** `examples/demo_signed_evidence.py` (E13) or a new
`examples/demo_attribution.py` shows two different `--initiated-by`
identities running sessions against the same server, and a `belay
verify-evidence` report correctly distinguishing which human/service
triggered which session. Add `docs/adr/0014-e14-identity-attribution.md`
documenting: why Belay does not implement authentication itself (scope
boundary — it trusts the identity the deployment's own front door
already asserts), the `initiated_by` vs `on_behalf_of` semantics, and how
this composes with E13 signing.

## E15 — Per-identity irreversible-action quota (not just per-call caps)

**Problem:** `PolicyEngine`'s existing `Cap` (E4) limits blast radius
*per call/plan* (e.g. "max 100 rows this action"). Nothing today limits
how many separate irreversible or high-risk approvals a given
human-or-agent identity can accumulate over a rolling window (e.g. "no
more than 20 irreversible actions per agent per day even if each one
individually looks small and gets approved"). This is the literal
enterprise governance ask: "I approved one bulk-delete, I did not approve
the agent doing that 200 times."

**Depends on E14:** quota is scoped per `initiated_by` identity, so this
entrega must land after (or alongside, sharing the same event-stamping
work) E14 — there is no meaningful per-identity quota without knowing
which identity a session belongs to.

**Constraint:** deterministic, ledger-derived (like E10's baselines —
reuse the same "read prior events, do not keep a second parallel
in-memory store of truth" philosophy), no LLM. Must compose with the
existing `deny > pause > allow` max-severity rule, not replace it.

**Design:**
- `belay/policy/quota.py`: `QuotaTracker` reads prior `policy_evaluated` /
  `approval_resolved` events from the ledger (via `belay/ledger/store.py`,
  same access pattern as E10's `BaselineStore`) filtered by
  `initiated_by` and a rolling time window (reuse the injectable `Clock`
  from `belay/clock.py`, E4 — do not read wall-clock time directly),
  counting approved/executed irreversible-effect actions.
- New policy dimension `quota` in `PolicyEngine.evaluate`, combined by the
  existing max-severity rule alongside `tools`/`quiet_hours`/
  `anomaly`(E10)/irreversible-default.
- `Defaults.quota` config: `enabled`, `window` (e.g. `"1d"`/`"7d"`,
  parsed relative to the injected clock), `max_irreversible_actions`,
  `verdict` (default `pause`) — sensible defaults so it's usable without
  hand-tuning, same spirit as E10's "works with zero manual
  configuration," though unlike E10 a quota number is inherently a policy
  choice an operator will often want to set explicitly; document the
  default chosen and why in the ADR rather than pretending zero-config
  is meaningful here the way it was for statistical anomaly detection.
- Reasons in `PolicyResult` must state the identity, the current count,
  the window, and the configured max, matching E10's explainability bar.
- Quota accounting must only count actions that were actually approved
  and executed (or auto-allowed), not ones that were denied/still
  pending — verify this distinction explicitly.

**Tests (TDD, red before green):**
- Below quota → `allow` contribution from this dimension; at/over quota →
  configured verdict (default `pause`), with an explanatory reason citing
  identity/count/window/max.
- Rolling window correctness with the injectable clock: an action just
  inside the window counts, one that has aged out of the window (per the
  clock) does not — test both boundaries explicitly.
- Per-identity isolation: two identities each individually under quota
  even though their *combined* count would exceed it — no cross-identity
  leakage (this is the direct counterpart to E14's no-cross-contamination
  test and E10's per-session isolation test).
- Only approved+executed actions count, not denied or still-pending ones
  — explicit test.
- Composition: quota firing alongside an existing cap/anomaly/irreversible
  verdict still resolves via max-severity correctly (extend E10's
  composition test pattern to include the new dimension).
- Property test (Hypothesis): for any sequence of N actions by one
  identity with a configured max of M, the (M+1)th irreversible action
  within the window always triggers the configured verdict, never
  `allow` — the core correctness guarantee for this entrega.
- CLI/end-to-end: a real session where an agent identity has its Nth
  bulk action paused purely by quota, no per-call cap involved.

**Exit:** `examples/demo_quota.py` (or an added step in an existing demo)
shows one identity being paused after exceeding its irreversible-action
quota within the configured window, entirely due to E15/E14, no
per-call `Cap` needed for that tool. Add `docs/adr/0015-e15-identity-quota.md`
documenting the window-rolling mechanism, why it composes with rather
than replaces E4's per-call caps, and the explicit default chosen (with
the honest caveat that, unlike E10, a meaningful default number is a
judgment call, not a statistically-derived zero-config value).

Update `CHANGELOG.md`'s `[Unreleased]` section alongside E10-E13's
entries for both E14 and E15 — do not remove or restructure existing
entries.

## Sequencing

E14 must land before E15 (E15 is scoped per-identity, which E14
introduces). Both touch `belay/proxy/lifecycle.py` and
`belay/ledger/model.py` (E14) plus `belay/policy/engine.py` (E15, on top
of E14's identity field) — build sequentially in the same working tree,
not in parallel, to avoid two agents editing the same files
concurrently. Run the full test suite + `belay-conformance ... --level 3`
after each lands.
