# ADR 0023: Native Agent Gate per-OS-user quota (R1, fourth slice)

## Status

Accepted, implemented (opt-in, off by default).

## Context

E15 (`belay/policy/quota.py::QuotaTracker`) gives the MCP proxy a
per-identity rolling cap on approved-and-executed irreversible actions,
independent of any per-call `Cap`. The Native Agent Gate had no
equivalent at all -- an audit found two blocking reasons this couldn't be
a quick reuse, not a design shortcut:

1. **No identity.** E15's identity is `session_started.initiated_by`, an
   explicit `--initiated-by` string the MCP proxy requires at session
   start (E14: "an unattributed session must be a deliberate, loud
   choice... never a silently-defaulted blank"). The hooks world has no
   `--initiated-by` concept anywhere -- `belay hooks install`/`hooks run`
   never ask for one.
2. **Different ledger shapes.** `QuotaTracker.count()` reads
   `plan_created` (for `reversibility`), `policy_evaluated` (for
   `verdict`), `step_committed`, and `approval_resolved` events keyed by
   MCP `plan_id`. Hook events are `hook_pre_tool_use`/`hook_post_tool_use`
   with a completely different payload shape -- none of those event types
   exist in the hooks world.

Both needed a real decision, not a lift-and-shift.

## Decision

**Identity = `HookEvent.os_user`.** Obtained from the OS itself
(`belay/supervisor/protocol.py::local_os_user`), independent of any
agent-supplied payload -- the same tamper-resistance property E14's
`initiated_by` has, just sourced differently because the hooks world has
no explicit attribution flag to require. This is a real, deliberate,
documented substitution, not a silent assumption: it answers "which OS
user is running this agent" rather than E14's "who authorized this
session," which is the most honest identity concept the Native Agent Gate
actually has available today.

**A parallel tracker, not a reuse.** `belay/hooks/quota.py::HookQuotaTracker`
reads `hook_pre_tool_use` events (now carrying `os_user`, added to
`gate.py::pre_event_evidence`'s payload -- ledger events are `extra:
"allow"`, spec §14, so this is a safe additive change) and
`approval_resolved` events (written by `belay hooks approvals approve`),
counting ones where `verdict == "deny"` (i.e., paused) and the
`approval_id` was later resolved to `"approved"`, within a rolling
window. Same two-pass shape as `QuotaTracker` (collect approved IDs, then
count matching events), deliberately parallel in spirit, not code.

**Semantics also diverge on purpose.** E15 escalates an otherwise-`allow`
irreversible action to `pause` once volume crosses the threshold. In the
hooks world this doesn't apply: everything that would need a quota check
already pauses by default (an unrecognized Bash command, a non-read-only
native MCP call, an oversized file edit) -- there is no "auto-allowed
irreversible action" for quota to catch. So `belay/hooks/quota.py::QuotaConfig`
escalates the *next* level instead: `pause` -> hard `deny`, once an
identity has accumulated too many *approved* actions in the window. The
check only ever gates a **brand-new** pending item (right before
`queue.request()` in `evaluate()`, `evaluate_mcp_call()`, and
`evaluate_file_edit()`'s oversized-file branch) -- it never touches an
existing pending/approved/rejected lookup.

**Configuration mirrors ADR 0021's `--contracts` pattern exactly:**
`belay hooks install --quota-max <N> --quota-window <window>` (e.g. `1d`,
`12h`), validated at install time (`parse_window`, reused as-is from
`belay/policy/quota.py` -- a pure string parser with no MCP coupling),
persisted to a small JSON pointer file
(`SupervisorIdentity.quota_config_path`), loaded once by
`Supervisor.__init__` into a `QuotaConfig | None` (`None` — the default —
is fully unchanged legacy behavior; a missing or corrupt config file
falls back to `None` rather than crashing the supervisor or fail-closed
denying everything, same posture as `_load_contract_set`).

## Consequences

- An operator can now cap how many risky hook-gated actions one OS user
  accumulates per window, the same protective spirit as E15, without
  claiming an identity model the hooks world doesn't have.
- Off by default; every install that doesn't pass `--quota-max` is
  unchanged.
- Does not address Bash's remaining gap (still a static classifier, no
  `PolicyEngine`) or anomaly-baseline tracking -- both still open R1
  scope. Anomaly baselines (`belay/policy/baseline.py`) face the exact
  same two blockers quota did (identity, ledger shape) and could
  plausibly follow this same `os_user` + parallel-tracker pattern in a
  future slice, not attempted here.
- Known minor gap, not fixed in this slice: `belay hooks uninstall` does
  not clear `contracts_pointer_path` or `quota_config_path` -- a
  subsequent `hooks install` without `--contracts`/`--quota-max` leaves a
  stale prior config in place until the pointer files are removed by
  hand. Pre-existing since ADR 0021; not introduced here, not resolved
  here either.

## Testing

`tests/hooks/test_quota.py` (`HookQuotaTracker`/`QuotaConfig` unit level:
counts only approved actions, never a different `os_user`, never an
`allow`-verdict event even with a stray `approval_id`, window-boundary
correctness). `tests/hooks/test_gate.py::TestQuotaEnforcement` (all three
pause paths hard-deny without queuing when exceeded; below max queues
normally; `quota=None` is unchanged; an already-pending item is untouched
by quota). `tests/supervisor/test_quota_config_loading.py` (the
pointer-file load path: absent, valid, missing key, invalid window,
malformed JSON). `tests/cli/test_hooks_lifecycle.py::TestHooksQuota` (a
real CLI round trip: two real approvals fill the quota, a third brand-new
command hard-denies with no item queued; without `--quota-max`
configured, repeated approvals never hard-deny; invalid `--quota-window`/
`--quota-max < 1` are rejected at install time, nothing written).
