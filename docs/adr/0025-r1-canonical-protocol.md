# ADR 0025: R1.7 canonical transaction protocol (design + first slice)

## Status

Proposed (design). Four concrete slices implemented alongside this ADR:
R1.7.1, R1.7.2, R1.7.3, and R1.7.4 (all below, each narrower or
differently scoped than first sketched -- see each one's own note).
`TransactionReceipt`'s poststate-capture half specifically was found to
need new `SagaExecutor` instrumentation that doesn't exist today, and
remains unbuilt -- see R1.7.4's own section.

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

**`ActionEnvelope`** -- **built, R1.7.3** (`belay/action_envelope.py`):
`{surface, host, tool, args, session_id, cwd, repo_prestate_digest,
os_identity, event_id, monotonic_ns, wall_clock}`, plus
`from_hook_event(HookEvent) -> ActionEnvelope` and `from_mcp_call(...) ->
ActionEnvelope`, proving both engines' per-call inputs already normalize
into one shape. Deliberately not wired into any production decision
path in this slice -- see R1.7.3's own section below for why the
originally-sketched next step (`ActionPlan`/`PolicyEngine` reuse) was
retired instead of built alongside it.

**`ActionPlan`** -- reuse `belay/planner/model.py::Plan` as-is; no new
type. The originally-sketched follow-up (making the hooks path's
`evaluate_file_edit`/`evaluate_mcp_call` call the real
`Planner.plan()`/`PolicyEngine.evaluate()`) was investigated as part of
R1.7.3 and found to be the wrong direction as sketched -- see R1.7.3's
own section below. Bash stays a deliberately separate problem (no stable
tool identity to resolve a `Contract` against at all) -- already flagged
in R1.6's own review as needing a `ShellEffectPlanner`, sequenced as
R1.9 in the broader roadmap, out of scope for R1.7 regardless.

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

**`TransactionReceipt`** -- **`policy_hash` half built, R1.7.4** (extends
`belay/ledger/signing.py::SignedEvidence` rather than inventing a
parallel receipt format, as sketched). The poststate-capture half was
investigated and found to need genuinely new `SagaExecutor`
instrumentation, not a field addition -- see R1.7.4's own section below
for why, and what's still open.

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

### R1.7.3 (this slice, revised scope): `ActionEnvelope` only, `ActionPlan` sketch retired

A focused investigation into the originally-sketched follow-up (make
`belay/hooks/gate.py`'s `evaluate_file_edit`/`evaluate_mcp_call` build a
real `Plan` via `Planner.plan()` and evaluate it through
`PolicyEngine.evaluate()`, replacing `gate.py`'s bespoke contract-presence
check) found four concrete reasons this specific idea does not hold up,
before any code was written:

1. `Planner.plan()` treats `contract is None` as "the caller already
   applied the default rule" and returns a *permissive* `Plan`
   (`reversibility="reversible"`, `effects=[]`) -- the real
   `contract_missing` deny logic lives entirely in
   `belay/proxy/lifecycle.py::resolve()`, before a `PlanningSession` is
   ever built. Swapping in a bare `Planner.plan()` call would make hooks
   **more permissive** than today for "ContractSet configured, tool
   unresolved," unless `gate.py` keeps re-implementing `resolve()`'s
   exact branching in front of it anyway -- which means the bespoke
   check doesn't disappear, it just duplicates a different function.
2. `Planner.plan()` is `async def`; the entire hooks call path
   (`Supervisor._decide_pre`/`_decide`/`handle_hook_event`/
   `_handle_request`, `belay/supervisor/server.py`) is deliberately
   synchronous with no event loop underneath it anywhere. Bridging via
   `asyncio.run(...)` is mechanically possible but a genuinely new
   pattern for this call path.
3. `PolicyEngine.evaluate()`'s anomaly and quota dimensions would go
   **permanently inert** on the hooks surface (not just slow to warm
   up): quota keys on a `session_started.initiated_by` event hooks never
   writes; anomaly keys on `plan_created` events
   (`belay/policy/baseline.py::BaselineStore.stats()`) hooks also never
   writes. Making them real is a separate piece of work -- mirroring
   `plan_created`/`session_started`-shaped events into the hooks ledger
   -- which is actually **R1.10's job** ("shared quota/anomaly store"),
   not something this slice gets for free.
4. This directly contradicts the project's own prior, deliberate
   architecture decision: **ADR 0023** already examined this exact
   identity/ledger-shape mismatch for quota, chose a parallel,
   hooks-native tracker (`HookQuotaTracker`, keyed on OS user) over
   reusing `PolicyEngine`/`belay.policy.quota.QuotaTracker` directly, and
   explicitly named anomaly baselines as facing "the exact same two
   blockers" and a candidate for the **same parallel-tracker pattern** in
   a future slice -- the opposite direction from what this sketch
   proposed.

**Decision**: only `ActionEnvelope` (see the "Canonical types" section
above) was built this slice -- a pure additive type + two conversion
functions (`belay/action_envelope.py::from_hook_event`/`from_mcp_call`),
called from no production decision path, zero behavior change. Whether
hooks should ever reuse `PolicyEngine` directly, or instead get its own
parallel anomaly tracker (matching ADR 0023's already-chosen pattern for
quota), is deferred until **after R1.10** gives hooks a real
`plan_created`/`session_started`-shaped ledger presence to evaluate that
question against -- not assumed now, in either direction.

### R1.7.4 (this slice, split scope): `policy_hash` on `SignedEvidence`, poststate deferred

`SignedEvidence`'s existing fields (`set_hash`, `initiated_by`,
`on_behalf_of`) are each derived from ledger events and cross-checked at
verify time -- not passed in from outside and trusted. `policy_hash`
follows the identical pattern rather than being bolted on differently:

- `belay/proxy/lifecycle.py::Lifecycle` now computes `self._policy_hash`
  once in `__post_init__` (previously computed inline, only for
  `ApprovalStage`'s R1.7.1 use) and folds it into `session_started`'s
  payload in `start_session()` -- inside the payload dict, not a
  dedicated `Event` field, matching how `tool_count`/`intent_contract_hash`
  already ride there (events are `extra="allow"`, spec §14, so this
  needs no schema change to `Event`/`EventRow` at all).
- `belay/ledger/signing.py` gains `_policy_hash_from_events()` (mirrors
  `_identity_from_events`), a `policy_hash` field on `SignedEvidence`,
  and the same signed-summary + verify-time cross-check treatment
  `initiated_by`/`on_behalf_of` already get -- editing a bundle's stated
  `policy_hash` without re-signing now fails at the `signature` stage,
  proven by `test_tamper_e_policy_hash_edited_without_resigning_fails_signature`.
- Bundles signed before this change (or any session not started via
  `Lifecycle`) simply have `policy_hash=None` throughout -- same
  established precedent as `initiated_by`/`on_behalf_of`'s own addition
  (E14): no `schema_version` bump, no special-casing old bundles, since
  none of the prior fields did either.

**Poststate capture was investigated and deferred, not built.** Today's
closest thing -- `state_captured` (a contract's `capture` block) --
only ever runs *before* the tool call, and `result_recorded` holds
whatever the tool itself returned, which is not the same claim as "the
resource's state afterward, captured the same way `capture` captures
the before-state." Adding a real poststate capture would mean
`SagaExecutor.run_step` gaining a genuinely new stage (re-running the
contract's `capture` tool *after* execution too, mirroring the existing
before-capture mechanism) -- a real behavioral addition to the six-stage
saga lifecycle, not a field added to a bundling type. That's
separately-scoped work, sequenced after this slice, not assumed to be
small.

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
- `belay/action_envelope.py::ActionEnvelope` now exists as a real,
  tested type both engines' per-call inputs provably normalize into --
  but it is not wired into any production decision path, and
  `ActionPlan` production (making hooks build a real `Plan`/`PolicyResult`)
  was investigated and explicitly retired as sketched, not built in a
  different shape -- see R1.7.3's own section above for the four reasons
  and the ADR-0023-aligned re-sequencing.
- `belay/ledger/signing.py::SignedEvidence.policy_hash` now lets a
  verifier confirm, offline and cryptographically, which policy config
  actually governed a session -- the same tamper-evidence guarantee
  `initiated_by`/`on_behalf_of` already had, extended to policy.
- Does **not** yet produce a real cross-engine `OutcomeEvidence` type, or
  a poststate-capturing `TransactionReceipt` -- both remain real,
  separately scoped follow-up work, not silently implied done by this
  slice's title. R1.7.2 specifically turned out to be narrower than
  first sketched (see its own section above) -- the three step-outcome
  status types (`RecoveryOutcome`/`StepStatus`/`OutcomeStatus`) remain
  three distinct types on purpose, not merged. R1.7.4's poststate half
  needs a genuinely new `SagaExecutor` stage, not a field addition (see
  its own section above).
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

`tests/test_action_envelope.py` (new, R1.7.3): `from_hook_event` against
a real `normalize()`-produced `HookEvent` asserts every field maps
correctly; `from_mcp_call`'s `event_id` is asserted to exactly match the
`f"{session_id}:{step_seq}"` string `ApprovalStage.check()` already
builds (R1.7.1) -- not just a similar-looking format, the literal same
string, proving the two engines' call-identity concept is already one
shape; a frozen-dataclass immutability check; and an explicit assertion
that `repo_prestate_digest`/`os_identity` stay `None` for the MCP side
rather than being papered over.

`tests/ledger/test_signing.py` (extended, R1.7.4): a session with a
`policy_hash` signs and verifies cleanly and the value round-trips into
`SignedEvidence.policy_hash`; a session with none stays honestly `None`
throughout (no crash, no false mismatch); a new tamper test
(`test_tamper_e_...`) mirrors the existing `event_count` tamper test --
editing `policy_hash` on an already-signed bundle without re-signing
fails at the `signature` stage. `tests/proxy/test_lifecycle.py`'s
existing `set_hash`-pinning test gained an assertion that
`session_started.payload["policy_hash"]` equals `lifecycle._policy_hash`
(the same value `ApprovalStage` already records per R1.7.1), tying the
two slices together rather than testing them in isolation. Full suite
green throughout (830 fast tests after this slice), `ruff`/`mypy` clean
repo-wide.
