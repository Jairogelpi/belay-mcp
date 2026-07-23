# ADR 0011: E11 -- Real SQL dry-run adapter

Fecha: 2026-07-23
Estado: aceptado

## Contexto

`docs/plan-v2.md`'s "E11 -- Real SQL dry-run adapter" section. Extends spec
§5.3's plan bases with a fourth, concrete basis, `sql_simulator`, sitting
between `native_dry_run` and the pre-existing `contract` fallback that E4
(ADR 0004) built. Additive on top of `v0.1.0`: no existing test, contract,
or precedence rule from E1-E4 changes behavior for a contract that does not
declare the new optional `sql` hint.

## Decisions

- **Precedence: `native_dry_run > sql_simulator > dry_run > contract`.**
  Spec §5.3 ranks bases "in decreasing strength" and ADR 0004 already fixed
  `native_dry_run` as strongest (the tool's own real dry-run beats anything
  Belay can infer from the outside) and `contract` as the weakest fallback
  (a static declaration, not an observation). `sql_simulator` sits directly
  above `contract`, below `native_dry_run`: it is a real, measured count --
  strictly better evidence than a declared guess -- but it is still Belay
  *simulating* against the tool's storage from the outside, not the tool
  itself reporting its own dry-run result (which may account for
  application-level logic a raw SQL statement cannot see, e.g. soft-delete
  filters, triggers, or ORM-level cascades). `dry_run` (a generic, non-SQL
  simulator) remains unimplemented, unchanged from E4, so the practical
  order this ships is `native_dry_run > sql_simulator > contract`.
  `Planner.plan()` builds the plan in *reverse* order (`contract` first,
  then `sql_simulator` if applicable, then `native_dry_run` if applicable),
  each stage unconditionally overwriting the previous stage's
  `effects`/`basis` -- so whichever stage runs last is the one that wins,
  which is exactly the strongest basis available for that call. This does
  not invert ADR 0004's decision: `native_dry_run` still always wins when
  present, exactly as documented there.
  Test: `tests/planner/test_planner.py::test_native_dry_run_takes_precedence_over_sql_simulator`.
- **`sql_simulator` only fires when both sides opt in.** A contract's `sql`
  hint (`belay/contracts/model.py::SqlHint`) is necessary but not
  sufficient -- `PlanningSession.sql_runner` must also be supplied by the
  caller (the proxy lifecycle, or a CLI/demo script that owns a real
  `sqlalchemy.Engine`). No `sql_runner` -> silently falls back to
  `contract` basis, same as `native_dry_run`'s existing `None`-callable
  fallback. This mirrors ADR 0004's "the planner does not re-derive
  anything the caller already resolved" principle: the planner does not
  open its own DB connections or own an `Engine` lifecycle; whoever wires
  up the session (the deployment) decides which tool's contract gets a
  real engine, same shape as wiring `native_dry_run`.
  Test: `tests/planner/test_planner.py::test_no_sql_runner_supplied_falls_back_to_contract_basis`.
- **The `sql` hint is one optional, additive field on `Contract`
  (`belay/contracts/model.py::SqlHint`), not a new top-level document or a
  parallel JSON Schema.** E1 built `belay/contracts/model.py` as a Pydantic
  mirror of spec Appendix A's JSON Schema (validated via
  `Contract.model_validate`, not a separate `jsonschema` library call), so
  "extend the JSON Schema additively" means exactly that: one new
  `sql: SqlHint | None = None` field. Every contract without it -- all of
  `examples/contracts/{fs,email}.yaml` and the non-`bulk_delete` tools in
  `crm.yaml` -- loads byte-for-byte unchanged; regression test
  `tests/contracts/test_model.py::test_old_contract_without_sql_field_still_loads_unchanged`
  plus the full existing `tests/contracts/` suite re-run green with zero
  modifications to those tests. `docs/spec.md`'s own Appendix A is
  deliberately left untouched here: it documents the frozen 0.1 spec text;
  `sql` is a plan-v2, post-0.1 addition documented in this ADR and in the
  `SqlHint` docstring instead, the same treatment E10's `AnomalyDefaults`
  got for the `anomaly` policy dimension.
- **`sql.statement` is validated by a small hand-written allow-list at
  *load* time, not a real SQL parser.** Reusing the project's existing
  posture on contracts as data, never code (spec §4.3's grammar for
  expressions, extended here to the SQL template): `SqlHint`'s validator
  requires the statement to be a single `SELECT`/`UPDATE`/`DELETE`
  statement (checked via a leading-verb regex and a "no `;` except one
  optional trailing one" rule) and rejects a small forbidden-keyword list
  (`DROP`, `ATTACH`, `PRAGMA`, `ALTER`, `CREATE`, `TRUNCATE`, `INSERT`,
  comment markers, etc.) -- enough to make "malformed/unsafe SQL in a
  contract" fail loudly as `contract_invalid` at `Contract.model_validate`
  time, the same moment every other Appendix A `allOf` violation fails,
  never silently deferred to first execution. This is deliberately not a
  general SQL grammar/parser (ponytail rung 1: the task needs "reject the
  unsafe cases", not "understand all of SQL") -- a determined contract
  author could still write a syntactically-valid-but-semantically-odd
  `SELECT`/`UPDATE`/`DELETE`; that is an accepted, documented limit, not a
  gap this ADR is silent about.
  # ponytail: allow-list regex, not a real SQL parser; upgrade to `sqlglot`
  # or similar only if a contract author needs a construct this rejects.
  Test: `tests/contracts/test_model.py::test_malformed_or_unsafe_sql_statement_is_contract_invalid_at_load_time`
  (parametrized over `DROP TABLE`, multi-statement injection via `;`,
  `PRAGMA`, `INSERT`, no-verb-at-all, and an empty statement).
- **Bind params reuse the existing expression language (spec §4.3), not a
  second templating syntax.** `SqlHint.params` maps each `:name` bind
  parameter to a Belay expression string (e.g. `"$args.before_year"`),
  parsed by the same `belay.contracts.expressions.parse`/`evaluate` that
  already governs `undo.args` and `conditions` -- so the same
  `expression_invalid` security boundary (no `eval`/`exec`, no dunder
  access, closed grammar) covers SQL bind values for free, with no new
  code to audit for injection. `_sql_effects()` in
  `belay/planner/planner.py` evaluates every `params` entry against a
  `{"args": ..., "context": {}}` scope before calling `sql_runner`, so the
  values sqlalchemy binds are always plain Python literals from a closed
  grammar, never a string interpolated into the SQL text itself --
  `sqlalchemy.text(...)` keeps the statement and the bound values separate
  all the way to the DBAPI, which is what actually prevents injection here
  (the allow-list above is a defense-in-depth belt, not the primary
  mechanism).
- **Mechanism: `BEGIN` (`engine.connect()` + `conn.begin()`), execute, and
  *always* `rollback()` in `finally` -- never `commit()`, on any path.**
  `belay/planner/adapters/sql.py::simulate_row_count` is the only place
  that touches a live DB connection for this feature. `SELECT` statements
  report the fetched row count; `UPDATE`/`DELETE` report the DBAPI's real
  `rowcount`, which SQLite computes as part of executing the statement
  even though the transaction is never persisted. `EXPLAIN` was considered
  (per plan-v2's "or EXPLAIN where ROLLBACK isn't safe") and rejected for
  v0.1: SQLite's `EXPLAIN QUERY PLAN` does not return an affected-row
  count at all (it describes the query plan, not row counts), so it would
  not satisfy "a real affected-row count" -- the real
  `BEGIN; ...; ROLLBACK` transaction is both simpler and the only one of
  the two options that actually answers the question a human approver
  needs answered. If a future dialect genuinely cannot support a rolled-
  back DML transaction, that would need its own adapter and its own ADR
  amendment, not a silent `EXPLAIN` substitution here.
- **Safety-critical test: kill the connection mid-simulation, DB provably
  unchanged.** `simulate_row_count`'s `finally: trans.rollback()` handles
  the well-behaved exception case, but the test that actually matters (per
  the task's explicit "treat this with the same rigor as E6's saga
  stage-order test") does *not* go through that `finally` at all: it opens
  a connection, begins a transaction, executes a real `DELETE`, and then
  closes the connection directly -- no `commit()`, no explicit
  `rollback()`, simulating a hard process kill mid-simulation. The
  assertion is on the DBAPI/SQLite guarantee this feature's safety
  actually rests on: an uncommitted transaction torn down without a commit
  rolls back on its own. A fresh connection afterward re-reads the same
  row count as before. This is real, not reasoned about:
  `tests/planner/adapters/test_sql.py::test_crash_mid_simulation_leaves_db_unchanged`.
- **Honest dialect-support note (do not overclaim Postgres).**
  `simulate_row_count`/`make_sql_runner` are written entirely against
  SQLAlchemy's dialect-agnostic `Engine`/`Connection`/`text()` API -- no
  SQLite-specific call anywhere in `belay/planner/adapters/sql.py`. In
  principle this should work unmodified against Postgres (`rowcount` on
  `UPDATE`/`DELETE`, `BEGIN`/`ROLLBACK` semantics, and bind-parameter
  passing are all standard DBAPI/SQLAlchemy behavior Postgres supports).
  **It has not been run against a live Postgres instance in this sandbox**
  (no Postgres server available here, and the task explicitly asks not to
  claim untested support works) -- only against real SQLite files via
  `sqlalchemy.create_engine("sqlite:///...")`, which is the one dialect
  this ADR claims as tested and working. Anyone relying on Postgres for
  `sql_simulator` should run `tests/planner/adapters/test_sql.py` against
  a real Postgres DSN before trusting the row counts in production; that
  verification is not part of this entrega.
- **`EffectEstimate.basis` gains the `"sql_simulator"` literal; no special
  case elsewhere.** `belay/planner/model.py::Basis` and `Plan`/
  `EffectEstimate` needed no other change -- `sql_simulator`-basis effects
  are plain `EffectEstimate`s like any other, `estimate=False` (this is a
  measured count, spec §5.3's "MUST NOT present contract-basis counts as
  exact" simply does not apply because this is not the `contract` basis),
  and `_confidence()` treats `sql_simulator` the same as `native_dry_run`
  (`"high"`, absent unknowns) -- both are real observations, not
  declarations. `belay/policy/engine.py` needs zero changes: it already
  reads `EffectEstimate.upper_bound()`/`.count` generically regardless of
  `basis`, which is the whole point of integrating through the existing
  model instead of adding an adapter-specific code path (task requirement:
  "the planner doesn't need a special case for this adapter vs. the
  `contract` adapter").

## Referencias

- `docs/spec.md` §5.3 (plan bases), §4.3 (expression language), §12
  (compensation/TOCTOU security considerations -- the rollback-never-commit
  argument above is this feature's analogue of "an undo that would exceed
  caps also pauses": a dry-run that would ever persist is not a dry-run).
- `docs/plan-v2.md` "E11 -- Real SQL dry-run adapter".
- `docs/adr/0004-e4-planner-policy.md` (the precedence and
  `PlanningSession`-callable-injection pattern this ADR extends, not
  inverts).
- Code: `belay/contracts/model.py` (`SqlHint`, `_validate_sql_statement`),
  `belay/planner/model.py` (`Basis`, `SqlRunner`,
  `PlanningSession.sql_runner`), `belay/planner/planner.py`
  (`_sql_effects`), `belay/planner/adapters/sql.py`
  (`simulate_row_count`, `make_sql_runner`).
- Tests: `tests/contracts/test_model.py` (sql hint validation, additive
  regression), `tests/planner/adapters/test_sql.py` (real SQLite fixture,
  crash-mid-simulation, zero-matching-rows), `tests/planner/test_planner.py`
  (precedence, fallback).
- Example: `examples/contracts/crm.yaml` (`crm.bulk_delete`'s new `sql`
  hint), `examples/demo_sql.py` (a real, run-to-completion plan showing a
  real simulated row count paused for approval).
