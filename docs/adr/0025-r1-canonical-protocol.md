# ADR 0025: R1.7 canonical transaction protocol (design + first slice)

## Status

Proposed (design). Two concrete slices implemented alongside this ADR:
R1.7.1 (below) and R1.7.2 (below, narrower in scope than first sketched
-- see its own note). R1.7.3-R1.7.4 remain sequenced, not built yet.

## Context

R1.6 ("correctness lock", see `CHANGELOG.md`) closed six concrete gaps in
the Native Agent Gate (hooks) path, including `ApprovalQueue.consume()` —
a compare-and-swap single-use approval lease
(`belay/approvals/queue.py`), and `_prestate_digest` — a content-based
working-tree fingerprint folded into `repo_identity`
(`belay/hooks/claude_code_adapter.py`). A post-merge review correctly
judged R1.6 as real hardening, not "R1 done": Belay still runs two
separate decision engines --

- the **MCP proxy** (`belay wrap`/`belay run`, mediated: Belay itself
  makes the call) -- `belay/proxy/lifecycle.py::Lifecycle`, and
- the **Native Agent Gate** (`belay hooks install`, observed: Claude Code
  makes the call, Belay only decides whether it was allowed) --
  `belay/hooks/gate.py`.

The proposed next step is a canonical transaction protocol both engines
would eventually run through: `ActionEnvelope`, `ActionPlan`,
`CapabilityLease`, `OutcomeEvidence`, `TransactionReceipt`, plus a
`PROPOSED -> PLANNED -> AUTHORIZED -> EXECUTING -> OBSERVED -> VERIFIED ->
COMMITTED` state machine (with `DENIED`/`EXPIRED`/`FAILED`/
`INDETERMINATE`/`COMPENSATED` as alternate outcomes).

A full-repo exploration of the MCP proxy path (`belay/proxy/lifecycle.py`,
`belay/planner/`, `belay/policy/`, `belay/executor/saga.py`,
`belay/executor/recovery.py`, `belay/ledger/`, `belay/rewind/service.py`,
`belay/cli/causal.py`, `belay/cli/export_pr.py`) found this protocol is
genuinely large -- it touches nearly every module -- and that much of it
already exists, in embryonic and disconnected form, rather than needing
to be invented from nothing:

| Canonical concept | Existing analogue | Gap |
|---|---|---|
| `ActionEnvelope` | None -- `HookEvent` (`belay/supervisor/protocol.py`) and the MCP proxy's ad-hoc `(tool, args, session_id, cwd)` inputs are two independent shapes | Genuinely new; needs a shared parent both normalize into |
| `ActionPlan` | `belay/planner/model.py::Plan` (`plan_id`, `effects: list[EffectEstimate]`, `reversibility`, `policy_verdict`, `confidence`, `expires_at`) | Only the MCP proxy path produces one; hooks' `evaluate_file_edit`/`evaluate_mcp_call` do their own bespoke contract-presence check instead of calling `Planner.plan()`/`PolicyEngine.evaluate()` |
| `CapabilityLease` | `ApprovalQueue.consume()` + `ApprovalItem.consumed_by_event_id`/`consumed_at`/`consumed_by_host`/`consumed_policy_hash` (R1.6) | Only the hooks path uses it -- `belay/proxy/lifecycle.py::ApprovalStage.check()` still treats `state == "approved"` as an unconditional, unlimited-reuse pass. **This is the gap R1.7.1 (below) closes.** |
| `OutcomeEvidence` | `SagaExecutor`'s six ledger-event stages (`step_journaled` -> `state_captured` -> `tool_called` -> `result_recorded` -> `compensation_registered` -> `step_committed`), `belay/executor/recovery.py`'s `step_indeterminate` | "Indeterminate" exists as three uncoordinated string literals: `recovery.py:84-91` (producer), `rewind/service.py`'s `StepStatus`/`OutcomeStatus` (consumer), `cli/causal.py` (display consumer) -- never unified into one type |
| `TransactionReceipt` | `belay/ledger/signing.py::SignedEvidence` (hashed + Ed25519-signed session bundle), `belay/cli/causal.py::CausalNode` / `belay/cli/export_pr.py`'s proof-carrying-PR body (both already assemble prestate/plan/policy/test/compensation per step) | No policy-config-hash field per step, no poststate capture, and none of it covers hook-gated actions at all |

Attempting the full protocol in one session would mean touching proxy,
planner, policy, executor, rewind, ledger, hooks, and CLI simultaneously
-- exactly the kind of half-finished, many-modules-mid-refactor state
that is hardest to recover from if interrupted, and hardest to review as
one diff. Decision, confirmed with the user: write the design down
properly now, and land the single concrete, bounded, high-value slice
this exploration surfaced -- R1.7.1 -- alongside it. Everything else is
sequenced, not attempted yet.

## Decision

### Canonical types (target shape, not all built yet)

**`ActionEnvelope`** (not built this slice) -- proposed shape: `{surface,
host, tool, args, session_id, cwd, repo_prestate_digest, os_identity,
event_id | step_seq, monotonic_ns, wall_clock}`, a superset `HookEvent`
and the MCP proxy's per-call inputs both normalize into. Follow-up work
(R1.7.3) converts `HookEvent` into, or alongside, this shape without
changing the hooks path's existing wire behavior or breaking its tests.

**`ActionPlan`** -- reuse `belay/planner/model.py::Plan` as-is; no new
type. Follow-up work (R1.7.3) is making the hooks path's
`evaluate_file_edit`/`evaluate_mcp_call` call the real
`Planner.plan()`/`PolicyEngine.evaluate()` instead of `gate.py`'s own
bespoke contract-presence check, so both engines produce the same
`Plan`/`PolicyResult` shape. Bash stays a deliberately separate problem
(no stable tool identity to resolve a `Contract` against at all) --
already flagged in R1.6's own review as needing a `ShellEffectPlanner`,
sequenced as R1.9 in the broader roadmap, out of scope for R1.7.

**`CapabilityLease`** -- reuse `ApprovalQueue`/`ApprovalItem`'s
`consumed_by_event_id`/`consumed_at`/`consumed_by_host`/
`consumed_policy_hash` fields exactly as R1.6 built them; no new type.
**R1.7.1 (this slice) is the first non-hooks consumer** -- see below.

**`OutcomeEvidence`** -- R1.7.2 landed a first, narrower slice of this
than originally sketched above. Closer inspection while implementing it
found the three "step-outcome" literals were never actually the same
type wearing three names -- `RecoveryOutcome.status` (`reconciled` |
`indeterminate`), `RewindStepPlan.status`/`StepStatus` (`reversible` |
`irreversible` | `conditional_unmet` | `indeterminate` | `no_op`), and
`CompensationOutcome.status`/`OutcomeStatus` (`compensated` |
`verification_failed` | `compensation_failed` | `skipped` | `paused` |
`denied`) each answer a genuinely different question (did recovery
resolve this step? / what's this step's reversibility classification?
/ what happened when we tried to compensate it?) -- collapsing them into
one `Outcome` enum would conflate three distinct concerns, not unify one.
The real, narrower duplication was the bare string `"step_indeterminate"`
(and its siblings `"step_committed"`/`"step_failed"`) typed out
independently at every producer/consumer site with no compiler-enforced
link between them. **Fixed as R1.7.2**: `belay/ledger/model.py` now
exports `STEP_COMMITTED`/`STEP_FAILED`/`STEP_INDETERMINATE` as named
constants (additive aliases into `EVENT_TYPES`, spec §9.1's list left
untouched), referenced from every writer (`belay/executor/saga.py`,
`belay/executor/recovery.py`) and every classifying reader
(`belay/rewind/service.py`, `belay/cli/causal.py`,
`belay/proxy/lifecycle.py`'s `step_failed` appends) instead of the bare
strings. A real `OutcomeEvidence` type spanning both engines (hooks has
no equivalent to any of these three today) remains separately scoped,
larger follow-up work -- not attempted here, and not the same task as
"remove the string-literal duplication."

**`TransactionReceipt`** (not built this slice) -- extend
`belay/ledger/signing.py::SignedEvidence` with a `policy_hash` field
(reusing `belay/canonical.py::canonical_hash` over the `PolicyDoc` -- the
exact mechanism R1.7.1 introduces below for approval consumption, not a
new hashing scheme) and a poststate capture, rather than inventing a
parallel receipt format. Sequenced as R1.7.4.

### State machine

The MCP proxy's actual per-call ledger event sequence, read off
`belay/proxy/lifecycle.py::Lifecycle.govern_and_execute` and
`belay/executor/saga.py::SagaExecutor.run_step`, already **is** a
`PROPOSED -> ... -> COMMITTED` state machine -- it has just never been
reified as a named enum, only as "which ledger events exist for a given
`step_seq`" (exactly what `belay/rewind/service.py::build_plan` and
`belay/cli/causal.py::build_causal_graph` both already reconstruct after
the fact):

```
session_started, contract_set_pinned            (once per session)
  -> [step_failed(intent_contract_violation)]    (optional, halts here)
  -> [config_override(unsafe_passthrough)]       (optional)
  -> plan_created
  -> policy_evaluated
  -> [config_override(irreversible_default_relaxed)]   (optional)
  -> [step_failed(policy_denied)]                (deny verdict, halts here)
  -> [approval_requested]                        (pause verdict; returns
                                                    pending_approval, halts
                                                    here until approved)
  -> step_journaled -> [state_captured] -> tool_called -> result_recorded
  -> compensation_registered -> step_committed | step_failed
```

Formalizing this as a real `TransactionState` enum is deferred until a
genuine `OutcomeEvidence` type (spanning both engines, still R1.7.3+
scope) gives `indeterminate`/`compensated` a proper shared home --
R1.7.2 (below) only removed the string-literal duplication around the
existing three separate status concepts, it did not merge them into one,
so adding a `TransactionState` enum today would still have to pick one of
those three pre-existing, differently-shaped status types to build on.

### R1.7.1 (this slice): MCP proxy adopts the same Capability Lease

`belay/proxy/lifecycle.py::ApprovalStage.check()`'s `existing.state ==
"approved"` branch unconditionally set `proceed=True` -- the exact same
gap R1.6 closed for hooks: an approved `plan_id` allowed an unbounded
number of separate future action instances, not just the one situation a
human actually approved. Fixed by calling the same
`ApprovalQueue.consume()` already built for hooks, no changes to
`queue.py` needed:

- `ApprovalStage` gains a `policy_hash: str` bound at construction
  (mirroring how `PolicyStage` already binds a fixed `PolicyDoc` at
  construction -- the same existing pattern, not a new one).
  `Lifecycle.__post_init__` computes it once:
  `f"contracts={self.contract_set.set_hash};policy={canonical_hash(self.policy.model_dump())}"`.
- On `existing.state == "approved"`, `ApprovalStage.check()` now calls
  `queue.consume(existing.approval_id, f"{session_id}:{step_seq}",
  host="mcp", policy_hash=self._policy_hash)`.
- `f"{session_id}:{step_seq}"` as the lease's `event_id`: `step_seq` is a
  fresh, monotonically-incrementing per-call counter incremented
  unconditionally at the top of every `govern_and_execute` call, so every
  distinct invocation -- including a genuine "call the same tool+args
  again after it already executed once" replay -- gets a new claim
  identity. Unlike hooks (which need same-`event_id`-retried idempotency
  because `PreToolUse`/`PostToolUse` are separate calls for one action),
  the proxy's approval-check and execution happen inside the same
  `govern_and_execute` invocation, so there is no legitimate "redeliver
  the identical dispatch" case to accommodate here.
- On `ApprovalAlreadyConsumed`, raises `BelayError("idempotency_conflict",
  ...)` -- the closest existing spec §11 code (already means "reusing a
  resource as if it were still fresh" for the saga executor's own
  idempotency keys) rather than an 18th registered code.

### R1.7.2 (this slice, revised scope): named step-outcome constants

Implementing the `OutcomeEvidence` unification sketched earlier in this
ADR found the three "step-outcome" status types were never duplicates of
each other -- each answers a different question (see the `OutcomeEvidence`
entry above for the full comparison). The actual, narrower duplication
was the bare strings `"step_committed"`/`"step_failed"`/
`"step_indeterminate"` typed out independently at every producer and
consumer site, with no compiler-enforced link between them -- a typo at
any one site would silently break the connection. Fixed:

- `belay/ledger/model.py` gains `STEP_COMMITTED`/`STEP_FAILED`/
  `STEP_INDETERMINATE` constants -- additive aliases into `EVENT_TYPES`
  (spec §9.1's normative list, left untouched as a tuple), not a
  replacement for it.
- Every writer (`belay/executor/saga.py`'s committed/failed appends,
  `belay/executor/recovery.py`'s indeterminate append,
  `belay/proxy/lifecycle.py`'s three `step_failed` appends) and every
  classifying reader (`belay/rewind/service.py::build_plan`'s
  `committed`/`indeterminate` checks, `belay/cli/causal.py`'s status
  fold) now reference the shared constants instead of the bare strings.
- Pure refactor, zero behavior change -- confirmed by the full test suite
  passing unchanged (no existing test needed updating), plus two new
  sanity tests (`tests/ledger/test_model.py`) asserting the constants'
  string values and that each is a real member of `EVENT_TYPES`.

## Consequences

- Both engines now share one real piece of the eventual canonical
  protocol (`ApprovalQueue.consume()`), not just a design intention --
  the MCP proxy's approval replay gap is closed using the identical,
  already-tested-under-concurrency mechanism hooks uses.
- `consumed_by_host="mcp"` and a real `policy_hash` are now recorded for
  every MCP-side consumption, giving `belay approvals`/future receipt
  tooling a uniform audit trail across both engines.
- `belay/ledger/model.py::STEP_COMMITTED`/`STEP_FAILED`/`STEP_INDETERMINATE`
  are now the one place every step-outcome event type is spelled --
  future code should reference these rather than reintroducing bare
  string literals.
- Does **not** yet unify `ActionEnvelope`, `ActionPlan` production, or
  produce a real cross-engine `OutcomeEvidence` type -- those remain real,
  separately scoped follow-up work (R1.7.3-R1.7.4), not silently implied
  done by this slice's title. R1.7.2 specifically turned out to be
  narrower than first sketched (see its own section above) -- the three
  step-outcome status types (`RecoveryOutcome`/`StepStatus`/
  `OutcomeStatus`) remain three distinct types on purpose, not merged.
- `idempotency_conflict` is now used for two related-but-distinct things
  (the saga executor's upstream-call idempotency keys, and approval
  consumption) -- both mean "a resource that must be used at most once
  was presented as if still fresh," so this is a considered reuse, not
  an overload; revisit only if evidence emerges that operators need to
  tell the two apart programmatically.

## Testing

`tests/proxy/test_lifecycle.py`: new test proving a **third**
`govern_and_execute` call with the identical tool+args (after the
existing pause -> approve -> execute two-call flow) now raises
`BelayError(code="idempotency_conflict")` instead of silently
re-executing; the existing two-call approve-then-execute test needed no
changes (verified by reading it first -- the fix is additive to a third
call, not a behavior change to the first two); one test confirming
`consumed_by_host`/`consumed_policy_hash` land on the `ApprovalItem`
after a real `govern_and_execute` consumes it.

`tests/ledger/test_model.py` (new, R1.7.2): the three constants' string
values, and that each is a real member of `EVENT_TYPES`. Full existing
suite re-run unchanged (821 fast tests) to confirm the constant swap is
a pure refactor -- no test needed updating, since the actual event-type
strings written/read never changed.
