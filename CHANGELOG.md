# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.

## [Unreleased]

### Added

- **Statistical anomaly baselines (E10, plan-v2 §"E10"):** `belay/policy/baseline.py`
  -- deterministic, no-LLM, no-network per-session rolling mean/stddev
  (Welford's algorithm) computed from the ledger's own `plan_created`
  history. New `anomaly` policy dimension in `PolicyEngine.evaluate`,
  combined with `tools`/`quiet_hours`/irreversible-default by the same
  max-severity rule. Zero manual configuration required (`min_samples=10`,
  `z_score_threshold=3.0`, `verdict=pause` by default); cold-start below
  `min_samples` never blocks. `examples/demo_anomaly.py`,
  `docs/adr/0010-e10-anomaly-baselines.md`.
- **Real SQL dry-run adapter (E11, plan-v2 §"E11"):**
  `belay/planner/adapters/sql.py` -- new `sql_simulator` plan basis (spec
  §5.3), slotted `native_dry_run > sql_simulator > dry_run > contract`.
  Runs a contract's new optional `sql` capture/effect hint
  (`belay/contracts/model.py::SqlHint`, additive -- old contracts load
  unchanged) as a real `BEGIN; ...; ROLLBACK` transaction against a real
  SQLAlchemy `Engine` to get a genuine affected-row count, never
  committing on any path (verified by a crash-mid-simulation test with no
  explicit rollback). Bind parameters reuse the existing §4.3 expression
  language, no second templating syntax. SQLite tested and working;
  Postgres implemented via the same dialect-agnostic API but not verified
  against a live instance in this sandbox (see the ADR's honesty note).
  `examples/contracts/crm.yaml` (`crm.bulk_delete`'s `sql` hint),
  `examples/demo_sql.py`, `docs/adr/0011-e11-sql-dry-run.md`.
- **Counterfactual replay (E12, plan-v2 §"E12"):**
  `belay/ledger/counterfactual.py::run_counterfactual` -- "what would have
  happened if a human had decided differently at an approval/policy point,"
  computed entirely offline from the ledger: zero real upstream calls, zero
  mutation of the real session (a `CounterfactualBranch` holds only a
  read-only tuple of `Event`s, no `LedgerStore` handle at all). Reuses
  `belay.ledger.replay.replay()` for the real session's baseline final
  state and the existing `Basis` literal (E4/E11) for divergent steps'
  estimates, rather than duplicating either. Honesty rule (mirrors E7's
  `fully_rewound`): steps identical to reality are `unchanged` (the real
  recorded result); steps that diverge *because of* the override with a
  safe read-only estimate available are `diverged` with that `Basis` (or
  the branch's own `"simulated"` marker); steps with no safe way to
  re-derive an outcome are `unknown`, never fabricated. `belay
  counterfactual <session_id> --at-step <n> --override '<json>' [--json]`.
  `examples/demo_counterfactual.py`,
  `docs/adr/0012-e12-counterfactual-replay.md`.
- **Cryptographically signed, offline-verifiable evidence (E13, plan-v2
  §"E13"):** `belay/ledger/signing.py` -- Ed25519 (`cryptography`, no
  hand-rolled crypto) `SigningKey` persisted to an operator-controlled file
  (never inside the SQLite ledger). `sign_session` reuses `verify_chain`
  (E2) for the chain's terminal hash and `belay/canonical.py` for the
  signed summary, never a second chain-recomputation or canonicalization.
  `verify_evidence` is a pure function needing only the exported bundle:
  reports the *precise* failing stage (`chain` / `coherence` / `signature`
  / `summary_mismatch`), matching `verify_chain`'s existing per-index
  precision instead of an opaque pass/fail. New CLI: `belay keygen`,
  `belay verify-export <session_id> --key <path> -o <file>`, `belay
  verify-evidence <file> [--pubkey <path>]` -- the last needs zero database
  access, zero network, tested in a directory with no `belay.db` present at
  all. Covers all four tamper scenarios (payload byte flipped, re-signed
  with a different key, summary fields edited without re-signing, events
  appended after signing) plus a Hypothesis property test that any
  single-byte flip in the embedded events always fails verification.
  `belay verify` (E2's unsigned path) is completely unaffected.
  `examples/demo_signed_evidence.py`,
  `docs/adr/0013-e13-signed-evidence.md`.
- **Identity attribution: who told the agent to do this (E14, plan-v2
  §"E14"):** `initiated_by`/`on_behalf_of` promoted to named, typed
  `belay/ledger/model.py::Event` fields (`initiated_by` required-in-spirit,
  `on_behalf_of` optional) -- an externally-asserted identity Belay trusts
  from its deployment's own front door, never a login system Belay builds
  itself (scope boundary, see the ADR). `Lifecycle.start_session` now
  requires `initiated_by` (an accidental omission is a loud `TypeError`,
  never a silently-blank session); bound once on `session_started` rather
  than repeated per event, surfaced session-wide via `belay/ledger/replay.py`'s
  `SessionState`. New CLI: `belay wrap`/`belay run --initiated-by
  <identity> [--on-behalf-of <identity>]`; `belay verify`/`belay
  verify-evidence` (E13) both surface `initiated_by`/`on_behalf_of` in their
  reports. E13 integration: `sign_session`'s signed summary now covers
  `initiated_by`/`on_behalf_of`, so tampering with who initiated a session
  is detected exactly like tampering with the chain/`event_count`
  (`SignedEvidence` also tightened to `extra="forbid"`, closing a gap the
  new Hypothesis property test found: silently renaming a bundle field used
  to fall back to its default instead of failing to parse). Regression: all
  7 pre-existing `start_session()` call sites across E3-E13 test/conformance
  fixtures updated to pass an explicit `"test-fixture"` identity -- no test
  was left silently broken by the new required parameter.
  `examples/demo_attribution.py`,
  `docs/adr/0014-e14-identity-attribution.md`.
- **Per-identity irreversible-action quota (E15, plan-v2 §"E15"):**
  `belay/policy/quota.py::QuotaTracker` -- a rolling-window count of one
  E14 `initiated_by` identity's approved-and-executed irreversible actions,
  read from the ledger across all of that identity's sessions (same
  "read the ledger, no parallel store" philosophy as E10's
  `BaselineStore`). New `quota` policy dimension in `PolicyEngine.evaluate`,
  combined with `tools`/`quiet_hours`/`anomaly`/irreversible-default by the
  same max-severity rule -- composes with, does not replace, E4's per-call
  `Cap`. Only actions that were actually approved (or auto-allowed) *and*
  executed count (`step_committed` proof); denied or still-pending actions
  never do. `Defaults.quota` (`QuotaDefaults`) ships `enabled=False` by
  default -- unlike E10's statistically-derived zero-config baseline, a
  quota number is an operator's own risk judgment call, not something
  Belay can derive from data alone (honest caveat documented in the ADR).
  `reasons` cite the identity, current count, window, and configured max.
  `examples/demo_quota.py`, `docs/adr/0015-e15-identity-quota.md`.
- **Blast-radius self-explanation returned to the agent (E16, plan-v2
  §"E16"):** `belay/policy/explain.py::explain(policy_result, plan,
  contract=None) -> Explanation` -- a pure formatting function, no new
  computation: every number in its output is traceable back to the real
  `PolicyResult.reasons` already computed by `PolicyEngine.evaluate`
  (caps/tools/quiet_hours/irreversible-default from E4, `anomaly` from E10,
  `quota` from E15). `Explanation` (`verdict`, `headline`, `dimensions`,
  `suggested_action`) now rides on every governed response the calling
  agent receives, not just what a human sees via `belay approvals
  list`/the ledger: `pending_approval` carries it inline,
  `policy_denied`/`approval_rejected`/`approval_expired` carry it in
  `BelayError.detail["explanation"]`, and `allow` responses get a minimal
  empty-dimensions `Explanation` too (for symmetry), folded additively into
  `CallToolResult.structuredContent` in `belay/proxy/server.py` without
  touching the upstream's own response shape. `suggested_action` is a
  deterministic, mechanical suggestion (never guessed) present only when a
  contract declares a `$args.<path>`-referencing narrowing argument via
  `conditions` (conditional contracts) or `sql.params` (E11) -- absent
  otherwise. Disclosure policy: full transparency, applied uniformly across
  every dimension (documented and justified against spec §12 in the ADR).
  Hypothesis property test confirms `explain()` never raises and never
  references a number absent from `reasons`, across real `PolicyResult`s
  from the real `PolicyEngine.evaluate()`. `examples/demo_self_explain.py`
  (a scripted agent that reads its own pause's `suggested_action`, narrows,
  resubmits, and gets `allow` -- zero human approval steps),
  `docs/adr/0016-e16-blast-radius-self-explanation.md`.

## [0.1.0] - 2026-07-22

First feature-complete release: an MCP proxy giving agents contract-based,
policy-gated, reversible tool execution, L3 conformant against
`docs/spec.md` (Belay Specification 0.1). Built entrega-by-entrega (E0-E9)
per `docs/plan.md`; see `docs/adr/` for the decision record of each.

### Added

- **Scaffolding (E0):** package layout, `pyproject.toml`, ruff/mypy/pytest
  configuration, pre-commit hooks, GitHub Actions CI, Alembic migrations.
- **Contracts + expression language (E1, spec §4):** `belay/contracts` —
  `parse`/`evaluate` for the closed-grammar expression language (no
  `eval`/`exec`), YAML/JSON contract loading with JSON-Schema validation,
  canonical JSON + `set_hash`.
- **Event ledger (E2, spec §9):** `belay/ledger` — append-only, hash-chained
  events, chain + coherence verification, deterministic replay, secret
  redaction. `belay verify`.
- **L1 MCP proxy + CLI (E3, spec §3, §4.6, App. C):** `belay/proxy`,
  `belay wrap` / `belay run`. Contract resolution, the default rule for
  tools without a contract, passthrough execution, full ledger recording
  over stdio against any standard MCP client.
- **Planner + policy engine (E4, spec §5, §6):** `belay/planner`,
  `belay/policy` — dry-run effect estimation (`contract` and
  `native_dry_run` adapters), blast-radius caps, `deny > pause > allow`
  verdicts, plan expiration. `belay plan`.
- **Approvals (E5, spec §7):** `belay/approvals` — pending/approved/
  rejected/expired lifecycle, structural no-self-approval (no agent-facing
  approval surface), approver binding to `plan_id`. `belay approvals
  list|approve|reject`.
- **Saga executor (E6, spec §8):** `belay/executor` — the normative
  journaled/capturing/calling/result_recorded/compensation_registered/
  committed step cycle, idempotency keys, crash recovery from the ledger
  alone, conditional-undo re-checking.
- **Rewind (E7, spec §10):** `belay/rewind` — reverse-order compensation,
  session fencing across processes, honest `fully_rewound` reporting,
  `--dry-run` and `--skip-and-continue`. `belay rewind`. Closes L3
  conformance.
- **Public conformance suite (E8, spec §13):** `belay-conformance` — a
  target-agnostic pytest suite (`@conformance(level=...)`) driven by a
  ~6-method `ConformanceTarget` adapter, plus example contract packs
  (filesystem, CRM, email/irreversible).
- **Demo, docs, and portfolio polish (E9):** `examples/demo.py` (real,
  runnable reproduction of the `docs/plan.md` §10 scenario, `--oops`
  variant included), `docs/architecture.md` (full Mermaid component +
  lifecycle diagram), README badges/quickstart/comparison section,
  `CONTRIBUTING.md` + issue templates, `.github/workflows/release.yaml`
  (PyPI trusted publishing on tag push).

### Known gaps (tracked, not blocking v0.1.0)

- `belay approvals approve --narrow <filter>` is not implemented as CLI
  surface; the tested equivalent is re-planning with narrower args (spec
  §12, new `plan_id`) and approving that plan instead. See
  [ADR 0007](docs/adr/0007-e7-rewind.md), [ADR 0009](docs/adr/0009-e9-demo-docs-polish.md).
- `docs/traceability.md` (the spec-section -> test generator described in
  `docs/plan.md` §8) was not built in any entrega; spec MUST coverage is
  currently verified by reading the test suite, not a generated table.
- No PyPI release exists yet; publishing requires the maintainer to
  configure trusted publishing on PyPI first, then push the `v0.1.0` tag.
- No demo GIF/asciinema recording is embedded in the README; `asciinema`
  and `vhs` were not available in the sandbox this entrega was built in. A
  VHS tape script (`examples/demo.tape`) is checked in for the maintainer
  to render.
- The default `pytest` run (fast loop, `slow`-marked subprocess/integration
  tests deselected) finishes in ~25-30s but covers `belay/` at ~89%, just
  under the §0 90% bar; the full suite (CI's second `pytest` step, `-m ""`)
  covers ~93% but takes ~85-90s, over the §0 60s bar. The two §0 criteria
  are in tension as specified; CI runs both the fast loop and the full
  suite so neither speed nor coverage is silently dropped, but no single
  `pytest` invocation satisfies both numbers at once.
