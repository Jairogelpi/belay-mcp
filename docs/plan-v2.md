# Belay — Plan v2 (post-v0.1.0): E10 anomaly baselines, E11 SQL dry-run

Continuation of `docs/plan.md`'s methodology (SDD+TDD, one PR/commit per
entrega, English artifacts, no LLM anywhere in `belay/`, no `eval`/`exec`).
Both entregas are additive — they must not weaken any E0-E9 test or break
L3 conformance (`belay-conformance run --target belay --level 3` must still
pass after each).

## E10 — Statistical anomaly baselines (no manual thresholds)

**Problem:** today `PolicyEngine` only pauses/denies when a human has
pre-configured a `Cap` (e.g. "max 100 rows"). An agent doing something
50x its own normal behavior with no cap configured sails through as
`allow`. The demo should be able to say "it caught this on its own."

**Constraint:** fully deterministic, no LLM, no network call, no opaque
ML model — a rolling statistical baseline per (session or tool), computed
from the ledger's own history. Must be explainable: the `reasons` in a
`PolicyResult` must state the baseline value, the observed value, and the
deviation (e.g. "delete count 512 is 47.3x the trailing baseline of 10.8").

**Design:**
- `belay/policy/baseline.py`: `BaselineStore` reads prior `Event`s from the
  ledger (via `belay/ledger/store.py`) for effect-estimate counts per
  `(tool, effect_type)`, keeps a rolling mean/stddev (Welford's algorithm,
  streaming, no need to hold full history in memory).
- New policy dimension `anomaly`: evaluated in `PolicyEngine.evaluate`
  alongside the existing `tools`/`quiet_hours`/irreversible-default
  dimensions, combined by the same max-severity rule (`deny > pause >
  allow`).
- `Defaults.anomaly` config: `enabled: bool`, `min_samples` (don't flag
  before there's enough history — first N calls are baseline-building,
  never flagged), `z_score_threshold` (default e.g. 3.0), `verdict` (what
  to do when triggered — default `pause`, configurable to `deny`).
  Sensible defaults so it works with **zero manual configuration** — this
  is the whole point, "no rules manuales" — but must be able to be
  disabled per-tool via config the same way irreversible-default
  relaxation works (E4).
- Cold-start rule: with `min_samples` not yet reached, `anomaly` dimension
  contributes `allow` — never block on insufficient data.
- Must not double-count with existing caps: if a `Cap` already exists for
  the tool/effect and it's the one that fired, don't also fire `anomaly`
  redundantly — but if `anomaly` fires independently (no cap configured at
  all) that's the win condition. Document how they compose in the ADR.

**Tests (TDD, red before green):**
- Welford update correctness (property test vs. naive mean/stddev on
  random sequences).
- Cold start: fewer than `min_samples` calls never triggers `anomaly`,
  regardless of magnitude.
- Trigger: after baseline established (e.g. 10 calls averaging ~10 rows),
  a call estimating 500 rows → verdict `pause`, `reasons` includes the
  human-readable deviation explanation with actual numbers.
- No manual cap configured anywhere, still catches the outlier (the literal
  "me salvó solo" scenario) — a dedicated acceptance test with zero policy
  config beyond defaults.
- Composes correctly with existing cap/irreversible/quiet-hours dimensions
  (max-severity still wins across all of them together).
- Baseline is per-session-history from the ledger, not global in-memory
  state — verify by reading two independent sessions and confirming no
  cross-contamination.
- `belay plan`/`belay run` CLI paths still work; extend `belay policy`
  inspection if one exists, or `belay plan` output, to show the baseline
  context so a human approver can see *why* it paused.

**Exit:** `examples/demo.py` gets a new variant (or an added step) showing
a bulk action being paused purely by the anomaly baseline, no `Cap` in
`examples/contracts/` or policy config for that tool. Update
`docs/adr/0010-e10-anomaly-baselines.md`.

## E11 — Real SQL dry-run adapter

**Problem:** `Planner`'s `native_dry_run` (E4) only works if the wrapped
tool exposes a `<tool>.dry_run` sibling itself; the `contract` basis is a
declared/estimated effect, not a real count. Plan.md explicitly deferred
"the SQL simulator" as a future issue (§11). For a DB-backed tool, being
able to say "this will delete 8214 rows" from an actual `EXPLAIN`/dry
transaction, not a guess, is what makes the pause/approval step
trustworthy.

**Design:**
- `belay/planner/adapters/sql.py`: new dry-run basis `sql_simulator`,
  slotted into the existing precedence (`native_dry_run > sql_simulator >
  dry_run > contract` — confirm exact ordering makes sense against
  spec §5.3's existing precedence language in the ADR, don't silently
  invert it).
- Applies only to tools whose contract declares a `sql` capture/effect
  hint (new optional contract field, additive — must not break existing
  contract schema validation from E1, extend the JSON Schema with a new
  optional property, old contracts remain valid).
- Mechanism: wrap the real statement in `BEGIN; ... ; ROLLBACK` (or
  `EXPLAIN` where `ROLLBACK` isn't safe/available) against the actual
  target database to get a real affected-row count, never executing for
  real. Must work at minimum against SQLite (already a project dependency
  via SQLAlchemy) — support Postgres if it's a clean addition via
  SQLAlchemy's dialect-agnostic transaction API, but don't block E11 on
  multi-dialect polish if SQLite-real + Postgres-documented-as-untested is
  the honest state.
- Safety: the dry-run transaction must be provably rolled back even on
  crash mid-simulation (test: kill the connection mid-simulate, assert the
  DB is unchanged) — this is a security-relevant boundary, treat it with
  the same care as E6's saga stage-order test.
- Must integrate with `belay/contracts/expressions.py`'s existing
  `Scope`/evaluate model for the `estimate` numbers it returns, so the
  planner's `EffectEstimate` model doesn't need a special case for this
  adapter vs. the `contract` adapter.

**Tests (TDD, red before green):**
- Real SQLite fixture DB with real rows; a `DELETE ... WHERE` statement
  goes through `sql_simulator` → estimate matches the actual matching row
  count → DB is provably unchanged after (assert row count before ==
  after).
- Crash-mid-simulation leaves DB unchanged (explicit test, not just
  reasoned about).
- Precedence: a tool with both `native_dry_run` and a `sql` contract hint
  prefers `native_dry_run` (or whatever exact ordering the ADR settles on
  — test enforces it either way).
- Old contracts without the new optional `sql` field still load and
  validate exactly as before (regression test against E1's existing
  contract fixtures).
- Non-SELECT-affecting statements (e.g. an `UPDATE` with a `WHERE` matching
  0 rows) produce an honest `estimate: 0`, not an error.
- Malformed/unsafe SQL in a contract's declared statement template →
  `contract_invalid` at load time, never at execution time.

**Exit:** at least one `examples/contracts/` pack (extend `crm.yaml` or add
a new `sql-demo` example with a real SQLite-backed toy server) demonstrates
a plan showing a real simulated row count before a human approves.
`docs/adr/0011-e11-sql-dry-run.md` documents the transaction-rollback
safety argument and the dialect-support honesty note.

## Sequencing

E10 and E11 touch disjoint modules (`belay/policy/` vs `belay/planner/`)
and can be built in either order or in parallel; both must land before
either is considered "done" for this plan's purposes since the shared
`docs/adr/` numbering and `CHANGELOG.md` entries should reflect both. Run
the full test suite + `belay-conformance ... --level 3` after each to
catch cross-contamination early rather than at the end.
