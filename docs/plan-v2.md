# Belay — Plan v2 (post-v0.1.0): E10 anomaly baselines, E11 SQL dry-run, E12 counterfactual replay

Continuation of `docs/plan.md`'s methodology (SDD+TDD, one PR/commit per
entrega, English artifacts, no LLM anywhere in `belay/`, no `eval`/`exec`).
Entregas are additive — they must not weaken any prior test or break L3
conformance (`belay-conformance run --target belay --level 3` must still
pass after each).

## E10 — Statistical anomaly baselines (DONE, shipped)

Landed. See `docs/adr/0010-e10-anomaly-baselines.md`.

## E11 — Real SQL dry-run adapter (DONE, shipped)

Landed. See `docs/adr/0011-e11-sql-dry-run.md`.

## E12 — Counterfactual replay ("what if I had decided differently")

**Problem:** `belay rewind` answers "undo what actually happened." Nothing
today answers "what would have happened if I had approved/denied/narrowed
this differently at the moment of decision?" — without touching production,
without re-calling the real upstream, and without needing to have actually
run the alternate path live. Every competing gateway/observability tool
lacks this because none of them has a deterministic, pure ledger+replay
foundation to build it on (spec §9.4) — Belay already does (E2). This is
the single most differentiating feature available to build: an auditor or
on-call human can ask "why did this get approved, and what if it hadn't?"
entirely offline, from the ledger alone.

**Constraint:** fully deterministic, no LLM, no network calls, no real
tool/upstream invocation during a counterfactual run. A counterfactual
replay must NEVER be confused with or leak into the real session's ledger
— it produces its own separate, clearly-labeled report/branch, never
mutates or appends to the original session's event chain.

**Design:**
- `belay/ledger/counterfactual.py`: `CounterfactualBranch` /
  `run_counterfactual(events, at_step_seq, override, *, upstream_replay=None) -> CounterfactualReport`.
  - `events`: the real session's ledger events (read via
    `belay/ledger/store.py`, same as `replay()`).
  - `at_step_seq`: which decision point to fork at (must correspond to an
    actual `policy_evaluated` or `approval_*` event in the real ledger —
    forking at a point that never existed is a user error, reject it
    clearly, don't silently no-op).
  - `override`: the alternate decision (`verdict: allow|pause|deny`, or an
    alternate approval outcome/narrowed args) to substitute at that point.
  - Mechanism: re-run the **existing deterministic components** —
    `PolicyEngine`, `belay/contracts/expressions.py` evaluation, and the
    saga stage sequence from `belay/executor/saga.py` — against the
    branch, but replace any real upstream tool call with a **replay
    source**: by default, the *captured* snapshots and *recorded results*
    already in the original ledger for steps that didn't change; for any
    step whose behavior diverges *because* of the override (e.g. a step
    that would now execute but didn't originally, or vice versa), there is
    no real recorded result to replay — the branch must represent this
    honestly as `simulated` (using the same dry-run/estimate machinery
    from E4/E11, never a real call) rather than inventing a fake concrete
    result. This is the key honesty rule for this entrega, analogous to
    E7's `fully_rewound` honesty rule — do not let a counterfactual report
    claim a concrete outcome that was never actually observed or safely
    dry-run-estimated.
  - `upstream_replay`: optional override hook so a caller *can* supply a
    real read-only dry-run adapter (native_dry_run / sql_simulator from
    E11) for divergent steps, to get better-than-"simulated" estimates
    where safely available — but must default to the safe, no-real-call
    path if not supplied.
- `CounterfactualReport` model: which steps ran identically to the real
  session (`unchanged`), which ran differently due to the override
  (`diverged`, with a `basis: simulated|dry_run|sql_simulator` marker per
  E4/E11's existing `Basis` type — reuse it, don't invent a parallel one),
  and a final state comparison against the real session's actual final
  `SessionState` (from `belay/ledger/replay.py` — reuse `replay()`, don't
  duplicate its fold logic).
- CLI: `belay counterfactual <session_id> --at-step <n> --override '<json>'`
  prints the `CounterfactualReport` (human-readable + `--json` machine
  form). Read-only: must not require `belay run` to be live, must not
  touch the real upstream server, must not append anything to the real
  session's ledger (verify this with a test that snapshots the ledger row
  count before/after and asserts it's unchanged).

**Tests (TDD, red before green):**
- Forking at a real `policy_evaluated` event with an override verdict of
  `deny` where the real session was `allow` → downstream steps after that
  point are marked `diverged`/`simulated` (never fabricated as concrete
  results), and the report's final-state comparison correctly shows what
  differs.
- Forking with an override that matches what actually happened (a no-op
  override) → the report shows 100% `unchanged`, identical final state to
  `replay()` on the real events — this is the regression anchor proving
  the counterfactual engine agrees with reality when nothing is changed.
- Property test (Hypothesis): for any real session's event history and any
  no-op override at any valid decision point, `run_counterfactual` always
  reports `unchanged` for every step and a final state equal to
  `belay.ledger.replay.replay(events)` — the strongest correctness
  guarantee for this entrega, do not skip it.
- Immutability: running a counterfactual against a live SQLite ledger DB
  never appends a row to the real session's `events` table (assert exact
  row count before == after) and never opens a connection to any upstream
  MCP server (spy/mock the upstream transport layer and assert zero
  calls).
- Honesty: a step whose real outcome depended on data no longer safely
  re-derivable (e.g. the real call's result was never captured because it
  wasn't a read-only/capturable effect) must be reported as `unknown`, not
  guessed — this is the analogue of E7's irreversible-steps-stay-honest
  rule.
- Invalid fork point (a `step_seq`/event id that doesn't correspond to an
  actual decision event in that session) → clear error, not a silent
  empty report.
- CLI: `belay counterfactual` runs against a real SQLite fixture DB
  produced by an actual prior `belay run` session (reuse the existing
  stdio-subprocess fixture pattern from E3/E7's CLI tests), confirms the
  ledger is untouched afterward.

**Exit:** `examples/demo_counterfactual.py` (or an added step in one of the
existing `examples/demo*.py` scripts) runs the real `examples/demo.py`
bulk-delete-then-rewind scenario, then asks "what if the human had denied
instead of approved?" via `belay counterfactual`, and shows the branch
report — proving the feature end to end against a real session, not a
synthetic fixture only. Add `docs/adr/0012-e12-counterfactual-replay.md`
documenting: the honesty rule (simulated vs. unknown vs. unchanged), why
this reuses `replay()`/`PolicyEngine`/`Basis` rather than parallel
implementations, and the immutability guarantee's enforcement mechanism
(not just tested — architecturally, e.g. by never handing the branch a
handle to the real `LedgerStore.append`).

Update `CHANGELOG.md`'s `[Unreleased]` section alongside E10/E11's entries
(don't remove or restructure theirs).

## Sequencing

E12 depends on E10/E11 only incidentally (reuses `Basis` from E4/E11) and
is otherwise a new, disjoint module (`belay/ledger/counterfactual.py` +
`belay/cli/main.py` addition) — safe to build after E10/E11 land, which
they already have. Run the full test suite + `belay-conformance ... --level 3`
after landing to catch cross-contamination early.
