# Belay Specification

**Version:** 0.1-draft
**Status:** Draft for public review
**License:** MIT (specification text CC-BY-4.0)
**Editors:** —

The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY are to be interpreted as described in RFC 2119.

---

## 1. Purpose and scope

Belay defines an interoperable layer that makes AI-agent tool execution **declared, previewable, gated, and reversible by contract**. It specifies:

1. A **contract format** describing each tool's reversibility (§4).
2. An **effect-plan protocol** for dry-running tool calls (§5).
3. A **policy format** for blast-radius limits (§6).
4. **Approval semantics** for human-in-the-loop gating (§7).
5. **Execution semantics**: staged commits with compensation (§8).
6. A verifiable, append-only **event ledger** (§9).
7. **Rewind semantics** for undoing a session (§10).

Out of scope: authentication/authorization of agents (gateways do this), model-level guardrails, observability/tracing, and the transport internals of MCP itself. Belay composes with all of them.

A conforming implementation ("a Belay") is typically deployed as an MCP proxy: agents connect to Belay; Belay connects to the real tool servers. Nothing in this spec requires MCP — the contract and ledger formats are transport-agnostic — but MCP terminology is used throughout and Appendix C defines the MCP mapping.

## 2. Terminology

- **Tool** — a callable operation exposed by a server (e.g. `crm.create_record`).
- **Contract** — the declared reversibility metadata for one tool (§4).
- **Action** — one attempted tool call passing through Belay.
- **Effect** — one externally observable consequence of an action (§5.2).
- **Plan** — the predicted set of effects of an action, produced without executing it.
- **Session** — a correlated sequence of actions by one agent run, identified by `session_id`.
- **Step** — one committed action within a session, ordered by `step_seq`.
- **Compensation** — the concrete inverse call registered for a committed step.
- **Rewind** — executing compensations of a session in reverse order.
- **Ledger** — the append-only event log with verifiable evidence.

## 3. Architecture overview

```
Agent (LLM) ──MCP──▶ BELAY ──MCP──▶ tool servers
                       │
   ┌───────────────────┼──────────────────────┐
   │ contract registry │ policy engine        │
   │ planner (dry-run) │ approval router      │
   │ saga executor     │ rewind service       │
   └─────────┬─────────┴──────────────────────┘
             ▼
      event ledger (append-only, hash-chained)
```

Request lifecycle (normative):

1. **Resolve** the tool's contract. No contract ⇒ the default rule of §4.6 applies.
2. **Plan** the effects (always internally; exposed on demand via §5).
3. **Evaluate policy** against the plan (§6). Outcome: `allow | pause | deny`.
4. If `pause` ⇒ **park** in the approval queue (§7) until approved, rejected, or expired.
5. **Execute** as a staged step (§8): journal intent → call tool → capture result → register compensation → commit.
6. **Record** every transition in the ledger (§9).

Every numbered stage MUST emit its ledger event even when the outcome is a denial or an error.

## 4. Reversibility contracts

### 4.1 Contract document

Contracts are YAML or JSON documents, one per tool, collected in a contract set. Canonical form is JSON; YAML is an authoring convenience.

```yaml
belay_contract: "0.1"
tool: crm.create_record
summary: Creates a CRM record
reversibility: reversible          # reversible | irreversible | conditional
undo:
  tool: crm.delete_record
  args:
    id: "$result.id"
idempotent: false
idempotency_key: "$args.request_id"   # optional; enables safe retries
effects:                              # declared effect classes (§5.2)
  - type: create
    resource: crm.record
    count: "1"
verification:                         # optional post-undo check
  tool: crm.get_record
  args: { id: "$result.id" }
  expect: not_found
constraints:
  max_batch: 1
provenance:
  declared_by: "vendor|integrator|community"
  verified: false                     # true only after conformance tests (§13)
```

### 4.2 `reversibility` values

- `reversible` — an `undo` block MUST be present and MUST fully negate the declared effects.
- `irreversible` — `undo` MUST be absent. The action can never be compensated (e.g. `mail.send`). Policy defaults for irreversible actions are stricter (§6.4).
- `conditional` — reversibility depends on runtime state. The contract MUST include `undo` and MUST include `conditions`, a list of predicates over `$args`/`$result`/`$state` under which the undo is valid. When conditions are not met at execution time, the step is treated as irreversible and MUST be recorded as such.

### 4.3 The expression language

Argument mapping and predicates use a minimal, side-effect-free expression language:

- `$args.<path>` — arguments of the original call.
- `$result.<path>` — result payload of the original call.
- `$context.<key>` — Belay-provided context: `session_id`, `step_seq`, `timestamp`, `principal`.
- `$state.<path>` — a pre-execution snapshot captured by an optional `capture` block (see §4.4).
- Literals, `==`, `!=`, `<`, `>`, `in`, `and`, `or`, `not`, and `coalesce(a, b)`.

Implementations MUST reject contracts using any construct outside this grammar. No function calls, no I/O, no user-defined code in contracts: contracts are data.

### 4.4 Capture blocks (undo needs memory)

Many inverses need the *previous* value (`update_record` undone = restore old value). A contract MAY declare:

```yaml
capture:
  tool: crm.get_record
  args: { id: "$args.id" }
  as: before
undo:
  tool: crm.update_record
  args: { id: "$args.id", fields: "$state.before.fields" }
```

The capture call MUST be read-only (its own contract MUST declare `effects: []` or `readOnlyHint`), MUST execute before the main call within the same step, and its output is stored in the step's ledger record — it is part of the evidence.

### 4.5 Idempotency

If `idempotent: true`, retries of the identical call are safe. If `idempotency_key` is declared, Belay MUST deduplicate: a second execution with the same key within the same session MUST return the recorded result of the first without calling the tool again. This mirrors event-UUID idempotency in event-sourced systems and is REQUIRED for at-least-once transports.

### 4.6 The default rule (the point of Belay)

For a tool with **no contract**:

- If its MCP annotations declare `readOnlyHint: true` ⇒ treat as `effects: []`, allow.
- Otherwise ⇒ Belay MUST refuse to proxy the call with error `contract_missing` (§11), unless the operator has explicitly configured `unsafe_passthrough: true` per tool, which MUST be recorded in every affected ledger event.

Undeclared destructive capability is a configuration error, not a runtime surprise.

### 4.7 Contract set integrity

A contract set MUST carry a content hash (`set_hash`, SHA-256 over canonical JSON). Every ledger event referencing a contract MUST include `set_hash`, so evidence can prove *which* contract governed each step. Changing contracts mid-session is prohibited: a session pins the `set_hash` present at `session_started`.

## 5. Effect plans (dry-run protocol)

### 5.1 Plan request/response

A plan predicts effects without executing. Belay exposes:

```
plan(tool, args, session_id) -> Plan
```

```json
{
  "plan_id": "b3f1…",
  "tool": "sql.execute",
  "effects": [
    {"type": "update", "resource": "db.rows", "count": "~480",
     "estimate": true, "basis": "dry_run"}
  ],
  "reversibility": "conditional",
  "policy_verdict": "pause",
  "policy_reasons": ["rows_touched > 100"],
  "requires_approval": true,
  "confidence": "high",
  "unknown": []
}
```

### 5.2 Effect types

`create | update | delete | send | spend | execute | read`. Each effect has `resource`, `count` (exact or `~N` estimate with `estimate: true`), and optional `amount`/`currency` for `spend`, `recipients` for `send`.

### 5.3 Plan bases

`basis` declares how the prediction was obtained, in decreasing strength:

1. `native_dry_run` — the tool supports a real dry-run and it was used.
2. `dry_run` — Belay simulated (e.g. `EXPLAIN`/`SELECT COUNT(*)` for SQL).
3. `contract` — static declaration from the contract only.

Implementations MUST NOT present `contract`-basis counts as exact. A plan is honest about its own uncertainty: unpredictable aspects go in `unknown[]`, and policy treats unknown as worst-case (§6.3).

### 5.4 Plan/execute binding

Execution MAY reference a prior `plan_id`. If it does, Belay MUST re-validate that args are byte-identical; a mismatch is `plan_mismatch`. Plans expire (default 10 minutes) to bound TOCTOU windows; expired plans MUST be re-planned.

## 6. Policies

### 6.1 Policy document

```yaml
belay_policy: "0.1"
defaults:
  irreversible: pause              # allow | pause | deny
  conditional_unmet: pause
  unknown_effects: pause
caps:
  - match: { effect: update, resource: "db.*" }
    max_count: 100
    over: pause
  - match: { effect: spend }
    max_amount: { value: 50, currency: EUR }
    per: session
    over: deny
  - match: { effect: send }
    max_recipients: 10
    over: pause
tools:
  - match: "fs.delete_*"
    verdict: pause
quiet_hours:
  - between: ["00:00", "07:00"]
    scope: { effect: send }
    verdict: pause
```

### 6.2 Evaluation

Rules are evaluated in order; first match per dimension wins; the final verdict is the **most restrictive** across dimensions (`deny > pause > allow`). Every verdict MUST be recorded with the rule ids that fired (`policy_reasons`).

### 6.3 Unknowns are worst-case

If a plan contains `unknown[]` entries or `estimate: true` counts, caps MUST be evaluated against the upper bound; absent an upper bound, `defaults.unknown_effects` applies.

### 6.4 Irreversible default

Out of the box, `reversibility: irreversible` and unmet-`conditional` actions default to `pause`. Operators may relax per tool; relaxations are configuration, visible in the ledger.

## 7. Approvals

### 7.1 Queue semantics

A `pause` verdict parks the action as an approval item:

```json
{
  "approval_id": "ap_19…",
  "session_id": "s_7f3a",
  "plan": { "…": "…" },
  "requested_at": "…",
  "expires_at": "…",
  "state": "pending"    
}
```

States: `pending → approved | rejected | expired`. Transitions are one-way. Default expiry 30 minutes; an expired item MUST NOT be executable.

### 7.2 Approver identity

The approving principal MUST be authenticated by the embedding system and MUST be recorded (`approved_by`). Belay does not define auth; it defines that *anonymous approval is non-conforming*. An agent MUST NOT be able to approve its own actions through any tool Belay exposes.

### 7.3 Agent experience

While parked, the agent receives a structured, non-error response: `{"status":"pending_approval","approval_id":…,"poll_after_ms":…}`. On rejection: `approval_rejected` with optional human reason. Implementations SHOULD make these states visible to the model so it can re-plan instead of retry-hammering.

## 8. Execution semantics (staged commits)

### 8.1 Step lifecycle

```
planned → (pending_approval) → journaled → capturing → calling
        → result_recorded → compensation_registered → committed
   any → failed(reason)      committed → compensated | compensation_failed
```

Normative order within a step:

1. **journaled** — intent (tool, args, contract hash, plan) is durably appended *before* any external call.
2. **capturing** — `capture` block runs (if declared); snapshot appended.
3. **calling** — the real tool call, with `idempotency_key` if declared.
4. **result_recorded** — full result (or error) appended.
5. **compensation_registered** — the concrete inverse call is materialized by evaluating `undo.args` against `$args/$result/$state` **now**, and appended. Rewind never re-evaluates expressions against live state.
6. **committed**.

A crash between 3 and 4 leaves a journaled-but-unresolved step; on recovery Belay MUST reconcile via the idempotency key (re-issue and deduplicate) or, if impossible, mark the step `indeterminate` — a first-class state that rewind reports honestly.

### 8.2 Sagas (multi-step workflows)

A session's committed steps form a saga. There is no distributed lock and no two-phase commit across foreign APIs — compensation is the consistency mechanism, as in classic saga literature. On a declared workflow failure (agent aborts, step N fails irrecoverably), Belay MAY auto-unwind steps N-1…1 if the session was opened with `auto_compensate: true`; otherwise unwinding is an explicit rewind (§10).

### 8.3 Concurrency

Sessions are single-writer. Within a session, steps are strictly ordered by `step_seq`. Cross-session conflicts on shared external resources are out of scope (the external system's problem), but implementations SHOULD support optimistic guards in contracts via `conditions` on `$state` captures.

## 9. The ledger

### 9.1 Event model

Append-only sequence per deployment, partitioned by session. Event envelope:

```json
{
  "event_id": "uuid",
  "session_id": "s_7f3a",
  "step_seq": 12,
  "type": "result_recorded",
  "at": "2026-07-22T10:15:03.412Z",
  "payload": { "…": "…" },
  "set_hash": "sha256:…",
  "prev_hash": "sha256:…",
  "hash": "sha256:…"
}
```

Event types (complete for 0.1): `session_started`, `contract_set_pinned`, `plan_created`, `policy_evaluated`, `approval_requested`, `approval_resolved`, `step_journaled`, `state_captured`, `tool_called`, `result_recorded`, `compensation_registered`, `step_committed`, `step_failed`, `step_indeterminate`, `rewind_requested`, `compensation_executed`, `compensation_failed`, `rewind_completed`, `session_closed`, `config_override` (for `unsafe_passthrough` and policy relaxations).

### 9.2 Evidence

`hash = SHA-256(canonical(event without hash) || prev_hash)`. The chain makes tampering *evident*, not impossible — Belay claims verifiability, not immutability of the storage medium. A `verify` operation MUST recompute the chain and cross-check: every `committed` step has its journal, capture (if contracted), result, and registered compensation; every executed compensation references a committed step. This is the analogue of commit-evidence verification in event-sourced systems: coherence of persisted evidence, not a digital signature.

### 9.3 Redaction

Payloads MAY contain secrets. Implementations MUST support field-level redaction at write time declared in contracts (`redact: ["$args.password"]`); redacted fields are replaced by salted hashes so equality remains checkable. Redaction is recorded; silent mutation of past events is non-conforming.

### 9.4 Replay

Given the ledger alone, an implementation MUST be able to reconstruct: session state, every verdict with reasons, and the exact compensation set — with no access to the original tool servers. The ledger is the source of truth; everything else is cache.

## 10. Rewind

### 10.1 Request

```
rewind(session_id, to_step?, dry_run?, by)
```

- Scope: all committed steps with `step_seq > to_step` (default: all).
- `dry_run: true` returns the **rewind plan**: ordered compensations, plus the honest report — which steps are `irreversible`, `conditional`-unmet, or `indeterminate` and will remain.
- Rewind of a live session MUST first fence the session (no new steps).

### 10.2 Execution

Compensations execute in strict reverse `step_seq` order. Each execution is itself a mini-step: journaled, called, result recorded — in the same ledger. A failed compensation halts the rewind at that point by default (`halt_on_failure: true`); the operator may skip-and-continue explicitly, which is recorded. Post-conditions from `verification` blocks, when declared, MUST be checked and recorded.

### 10.3 Honesty requirements

A rewind result MUST enumerate: steps compensated, steps skipped and why, steps irreversible by contract, steps indeterminate. Implementations MUST NOT report a session as "fully rewound" if any step in scope was not compensated with passing verification. Partial truth, fully reported, is the contract.

## 11. Error model

Structured errors, machine-first: `{"code": "…", "detail": {…}, "retryable": bool}`.

Codes (complete for 0.1): `contract_missing`, `contract_invalid`, `expression_invalid`, `capture_failed`, `plan_expired`, `plan_mismatch`, `policy_denied`, `approval_required`, `approval_rejected`, `approval_expired`, `idempotency_conflict`, `step_indeterminate`, `compensation_failed`, `verification_failed`, `session_fenced`, `ledger_integrity_error`, `unsafe_passthrough_disabled`.

## 12. Security considerations

- **Prompt injection ≠ authorization.** Model output is untrusted input. Nothing an agent says can approve, relax policy, or edit contracts; those surfaces MUST NOT be exposed as tools to the protected agent. (Belay MAY expose read-only `plan`/`status` tools to the agent.)
- **Contract supply chain.** Contracts alter what "undo" means; a malicious contract is an attack. Contract sets SHOULD be signed, MUST be hash-pinned per session (§4.7), and changes SHOULD go through review — contracts are code-adjacent even though they are data.
- **TOCTOU.** Plans expire (§5.4); conditional contracts re-check `conditions` at execution time, not plan time.
- **Approver binding.** Approval UIs MUST display the plan actually bound to the approval (`plan_id`), not a paraphrase, to prevent bait-and-switch via re-planning.
- **Compensation blast radius.** Compensations pass through the same policy engine as forward actions. An undo that would exceed caps also pauses. Rewind is powerful; it is not exempt.
- **Ledger secrets.** See §9.3. Ledgers outlive sessions; treat them as sensitive data stores.

## 13. Conformance

Three levels, cumulative:

- **L1 — Contracts.** Enforces §4 (incl. default rule §4.6) and emits the ledger events of §9.1 for calls it proxies. May execute directly without plans/sagas.
- **L2 — Plans & policy.** Adds §5, §6, §7.
- **L3 — Sagas & rewind.** Adds §8, §10, and full ledger verification §9.2.

A public conformance suite accompanies the spec. Test categories map 1:1 to normative sections; every MUST above has at least one test. Highlights: refuse-undeclared (§4.6), capture-before-call ordering (§4.4/§8.1), worst-case unknowns (§6.3), no-self-approval (§7.2), materialized-compensation (§8.1.5 — expressions never re-evaluated at rewind), reverse-order rewind with honest partial reporting (§10), chain verification with a deliberately corrupted event (§9.2).

`provenance.verified: true` in a contract is only legitimate when the vendor's tool passes the L1 contract tests against a live instance.

## 14. Versioning

`belay_contract`, `belay_policy`, and event envelopes carry the spec version. 0.x versions may break; from 1.0, additive-only within a major. Unknown fields MUST be preserved (ledger) and MUST be rejected (contracts, policies) — evidence is tolerant, authority is strict.

---

## Appendix A — Contract JSON Schema (normative, abridged)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://belay.dev/schemas/contract-0.1.json",
  "type": "object",
  "required": ["belay_contract", "tool", "reversibility", "effects"],
  "properties": {
    "belay_contract": { "const": "0.1" },
    "tool": { "type": "string", "minLength": 1 },
    "summary": { "type": "string" },
    "reversibility": { "enum": ["reversible", "irreversible", "conditional"] },
    "undo": {
      "type": "object",
      "required": ["tool", "args"],
      "properties": {
        "tool": { "type": "string" },
        "args": { "type": "object" }
      }
    },
    "conditions": { "type": "array", "items": { "type": "string" } },
    "capture": {
      "type": "object",
      "required": ["tool", "args", "as"],
      "properties": {
        "tool": { "type": "string" },
        "args": { "type": "object" },
        "as": { "type": "string" }
      }
    },
    "idempotent": { "type": "boolean", "default": false },
    "idempotency_key": { "type": "string" },
    "effects": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["type", "resource"],
        "properties": {
          "type": { "enum": ["create","update","delete","send","spend","execute","read"] },
          "resource": { "type": "string" },
          "count": { "type": "string" },
          "amount": { "type": "object" },
          "recipients": { "type": "string" }
        }
      }
    },
    "verification": { "type": "object" },
    "redact": { "type": "array", "items": { "type": "string" } },
    "constraints": { "type": "object" },
    "provenance": {
      "type": "object",
      "properties": {
        "declared_by": { "enum": ["vendor", "integrator", "community"] },
        "verified": { "type": "boolean", "default": false }
      }
    }
  },
  "allOf": [
    { "if": { "properties": { "reversibility": { "const": "reversible" } } },
      "then": { "required": ["undo"] } },
    { "if": { "properties": { "reversibility": { "const": "irreversible" } } },
      "then": { "not": { "required": ["undo"] } } },
    { "if": { "properties": { "reversibility": { "const": "conditional" } } },
      "then": { "required": ["undo", "conditions"] } }
  ]
}
```

## Appendix B — End-to-end example

Agent asks to clean up stale CRM records.

1. `plan(crm.bulk_delete, {filter: "last_seen < 2024"})` → effects `[{delete, crm.record, "~512", estimate}]`, verdict `pause` (cap 100).
2. Agent receives `pending_approval`; human console shows the bound plan; human approves a narrowed re-plan (`~80` rows) instead.
3. Step 17 journaled → capture (`crm.export_records(filter)` → snapshot) → call → result (80 ids) → compensation registered (`crm.import_records($state.before)`) → committed.
4. Next day: wrong filter discovered. `rewind(s_7f3a, dry_run: true)` → plan shows 1 compensation, 0 irreversible.
5. `rewind(s_7f3a, by: "jairo")` → compensation executes, `verification` re-queries count, chain verifies, report: fully compensated.

## Appendix C — MCP mapping

- Belay is an MCP server to the agent and an MCP client to tool servers.
- `readOnlyHint: true` ⇒ implicit `effects: [read]` contract (§4.6).
- `destructiveHint: true` with no Belay contract ⇒ `contract_missing`.
- `idempotentHint` maps to `idempotent` but MUST be confirmed in the contract to be relied upon; hints are advisory, contracts are authoritative.
- `plan`, `status`, and `approvals` surfaces are exposed to *operators*, not to the protected agent, except read-only `plan`/`status` which MAY be agent-visible.
