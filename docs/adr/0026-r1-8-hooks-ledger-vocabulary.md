# ADR 0026: R1.8 reframed -- hooks needs a real step-lifecycle ledger presence first

## Status

Proposed (design only -- no code in this slice, by explicit choice: every
R1.7 sub-slice needed real rescoping once actually investigated, so this
one was investigated before committing to an implementation plan at all).

## Context

R1.6 (correctness lock) and all four R1.7 sub-slices (ADR 0025) are done
and CI-green. The next item on the roadmap, "R1.8: TransactionEngine
único," was described as moving contracts, the intent contract,
`PolicyEngine`, quotas, anomaly baselines, approvals, fencing, evidence,
and rewind onto one shared engine for both the MCP proxy
(`belay/proxy/lifecycle.py`, *mediated* -- Belay itself executes calls)
and the Native Agent Gate hooks path (`belay/hooks/gate.py`, *observed*
-- Claude Code executes, Belay only decides whether it was allowed to).

R1.7.3 already found one instance of a pattern worth naming precisely:
wiring hooks into `PolicyEngine.evaluate()` directly would leave its
anomaly and quota dimensions **permanently inert**, because both key on
ledger event types (`session_started`, `plan_created`) the hooks path
structurally never writes -- confirmed via `belay/policy/baseline.py`
(anomaly, keys on `plan_created`) and `belay/policy/engine.py`'s quota
check (keys on `session_started.initiated_by`). ADR 0025 deferred that
specific question to "after R1.10 (shared quota/anomaly store)."

Before writing an R1.8 implementation plan, two more pieces the
"TransactionEngine" vision explicitly names -- the intent contract, and
rewind -- were investigated the same way, to check whether R1.7.3's
finding was a one-off or a pattern.

## What was found: it is the same wall, every time

**Intent contracts are a complete, MCP-only subsystem with zero hooks
footprint.** `belay/intent/model.py`'s `IntentContract`
(`intent`/`acceptance` free text, `allowed_scope`/`forbidden_scope`
globs, `forbidden_tools`, `budgets.files_changed`) and
`belay/intent/enforce.py::check_intent_contract` are wired into
`belay/proxy/lifecycle.py::Lifecycle.govern_and_execute` as the very
first check (before `resolve`/plan/policy), with `--intent-contract`
flags on `belay run`, `belay export-pr`, and `belay learn --apply`. A
grep for "intent" (case-insensitive) across `belay/hooks/` and
`belay/supervisor/` returns **zero matches** -- not a missing wire-up,
a conceptually absent idea on that surface. `belay hooks install` has no
`--intent-contract` flag and nothing analogous.

**Rewind/undo are two genuinely independent systems, with no shared
code at all.** `belay/rewind/service.py::RewindService` builds a typed
`RewindPlan`/`RewindStepPlan` classification (`reversible`/
`irreversible`/`conditional_unmet`/`indeterminate`/`no_op`, reading
`STEP_COMMITTED`/`STEP_FAILED`/`STEP_INDETERMINATE` events) and
compensates via the shared `compensate_one` helper, itself policy-gated
through `PolicyEngine`. `belay/hooks/file_snapshot.py::SnapshotStore` is
a completely separate, self-contained content-addressed blob store:
`capture_before`/`record_after`/`restore` work directly against
`FileSnapshotRow` and blob files under `belay_home()/snapshots/`,
`restore()` returns a bare string, and none of `RewindPlan`,
`CompensationOutcome`, `compensate_one`, `PolicyEngine`, `ContractSet`,
or `is_fenced()` are imported anywhere in the file. The CLI commands
confirm the split is total: `belay hooks rewind`/`belay hooks list-edits`
(`belay/cli/main.py`) talk to `SnapshotStore` directly; `belay rewind`
instantiates a real `RewindService`. Neither reads the other's data.

**The root cause is the same single fact behind both findings, and
behind R1.7.3's quota/anomaly result**:
`belay/supervisor/server.py::Supervisor._decide` writes exactly two
ledger event types for every hook-gated call, always --
`hook_pre_tool_use` and `hook_post_tool_use` -- and *never* the MCP
path's rich step lifecycle (`plan_created`, `step_journaled`,
`state_captured`, `tool_called`, `result_recorded`,
`compensation_registered`, `step_committed`/`step_failed`/
`step_indeterminate`). Every machinery that would unify a piece across
both engines -- `PolicyEngine`'s quota/anomaly, `RewindService`'s
classification, and (if ever attempted) intent-contract enforcement's
own future ledger needs -- independently hits this identical wall: it
reads events hooks structurally never produces, because hooks only ever
*observes* a call Claude Code already decided to make, while MCP
*mediates* the call itself and can journal every stage of its own
execution as it happens.

## Decision

**"R1.8: TransactionEngine único," as originally phrased, is not
reachable as a single slice, or even as a small number of independent
per-subsystem slices.** Six-going-on-seven separate "unify X" ideas
(contracts -- already effectively shared, see Consequences;
intent contract; `PolicyEngine`; quota; anomaly baseline; fencing --
already shared; evidence; rewind) collapse into **one real prerequisite**
wearing seven names: hooks does not structurally produce the ledger
vocabulary any of this machinery reads.

**R1.8 is redefined**: give the hooks path a real step-lifecycle ledger
presence -- hooks-side events marking the equivalent of "a plan for this
call existed" and "this call committed/failed/was indeterminate,"
written *alongside* (never replacing) the existing
`hook_pre_tool_use`/`hook_post_tool_use` events, with **zero change to
any existing decision or gating behavior**. Concretely, reuse
`belay/ledger/model.py::STEP_COMMITTED`/`STEP_FAILED`/
`STEP_INDETERMINATE` (R1.7.2) for the outcome events rather than
inventing hooks-specific parallel names -- the whole point is to let
existing MCP-side machinery (`RewindService`'s classification,
`PolicyEngine`'s quota/anomaly checks) recognize hooks-path events
without needing to know two vocabularies. The exact shape of a
hooks-side "plan" equivalent (what stands in for `plan_created`'s
`effects`/`reversibility` when there's no `Contract`-driven `Planner`
run) is real design work for whoever picks up this slice, not decided
here -- likely candidates include: emitting a minimal `plan_created`-
shaped event carrying just `tool`/`args`/a best-effort reversibility
guess from the existing contract-presence check, or accepting a
deliberately thinner shape and teaching `RewindService`/`PolicyEngine`
to tolerate fewer fields. This is itself large enough to warrant its own
investigation-then-slice treatment, the same way R1.7's individual
pieces got, not assumed to land as a single bounded commit.

**What explicitly does NOT get unified even once this prerequisite
exists, and why**:

- **Rewind stays two systems, likely permanently**, unless a future
  slice explicitly decides file-snapshot restore should be re-expressed
  as `RewindService`-shaped compensations (a real behavioral change to
  how native edits get undone, not a free consequence of more ledger
  events existing). Not decided here.
- **Intent contracts are not scheduled for hooks at all.** Whether
  gating native Bash/file-edit calls by file-path scope/budget even
  makes product sense is a judgment call, not a technical gap this ADR
  can resolve by naming a prerequisite -- flagged as an open question
  for later, not assumed valuable enough to build.

**Sequencing after this ADR**:

- **R1.8** (redefined, this ADR): hooks gets a real step-lifecycle
  ledger presence. Not yet built.
- **R1.8.x** (after R1.8's prerequisite lands): revisit R1.7.3's deferred
  quota-unification question with real evidence this time; anomaly
  baseline for hooks; rewind unification *or* a documented, deliberate
  permanent split; `TransactionReceipt`'s poststate-capture half
  (R1.7.4's own deferred piece, which separately needs new `SagaExecutor`
  instrumentation regardless of hooks' ledger vocabulary).
- **R1.9** (unchanged from the existing roadmap): `ShellEffectPlanner`
  for Bash -- independent of everything above, since Bash's problem (no
  stable tool identity to resolve a `Contract` against at all) isn't a
  ledger-vocabulary question.
- **Intent contracts for hooks**: not scheduled; revisit only if a real
  product need for scope-gating native tool calls emerges.

## Consequences

- Prevents R1.8 from starting as an unbounded "unify everything" effort
  that would likely need mid-flight rescoping the same way each R1.7
  sub-slice did, at much higher cost given the larger surface.
- Correctly credits what's *already* shared and needs no new work:
  contract resolution (`ContractSet.resolve()`, used by both paths
  already), the `ApprovalQueue` (same SQLite-backed queue, and both
  paths use `ApprovalQueue.consume()`'s single-use lease as of R1.7.1),
  and session fencing (`is_fenced()`, a ledger fact both
  `Lifecycle.govern_and_execute` and `Supervisor._decide_pre` already
  check). "TransactionEngine único" undersold how much of this is
  already true, while overselling how close the rest is.
- Honest cost: even after R1.8's redefined prerequisite ships, quota and
  anomaly unification are *revisit-with-evidence* decisions, not
  automatic follow-ons -- ADR 0023's parallel-tracker pattern for hooks
  quota may still turn out to be the right call, not merely the
  current one.
- No code changes in this slice -- `ruff`/`mypy`/`pytest` are unaffected;
  this ADR is the entire deliverable.
