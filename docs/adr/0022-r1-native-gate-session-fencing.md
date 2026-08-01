# ADR 0022: Native Agent Gate session fencing (R1, third slice)

## Status

Accepted, implemented.

## Context

The MCP proxy fences a session before a real (non-dry-run) `belay rewind`
begins: `belay/rewind/service.py::RewindService.rewind()` appends a
`session_fenced` ledger event, and `belay/proxy/lifecycle.py`'s
`govern_and_execute` checks `is_fenced()` at the top of every call,
refusing new steps for a session once it's fenced (spec §10.1: "Rewind of
a live session MUST first fence the session"). This holds across
processes -- fencing is a ledger fact, not in-process state.

The audit comparing the MCP proxy against `belay/hooks/gate.py` (the
Native Agent Gate) found no equivalent for hook sessions at all: nothing
closes a hook session to new actions, ever. Once an operator decides a
session shouldn't continue (a concerning pattern noticed mid-session, a
rewind of a captured file edit, anything else), there was no way to stop
that host session's *future* Bash/file-edit/MCP calls from still being
evaluated normally.

## Decision

New CLI command: `belay hooks fence <host_session_id> --host <host> --db
<db>`. Writes a `session_fenced` event to the ledger under the same
`hook-<host>-<host_session_id>` key `belay/hooks/gate.py::ledger_session_id`
already computes for every hook event of that session -- refactored to
share a `session_key(host, host_session_id)` helper so both compute the
identical key from the identical inputs (tested explicitly:
`test_session_key_matches_ledger_session_id_for_the_same_event`).

`belay/supervisor/server.py::Supervisor._decide_pre` checks `is_fenced()`
(the exact same function the MCP proxy path already uses) once, before
dispatching to any surface-specific `evaluate_*()` -- so fencing closes
Bash, file edits, *and* native MCP calls uniformly, in one place, rather
than needing the check duplicated in each. No `unfence` command: fencing
is meant to be a hard stop for that session, not a pause; start a new
session with the agent instead.

A real bug was found and fixed while testing this: `_hooks_ledger_for`
(the CLI helper `belay hooks fence`/`hooks approvals approve`/`reject`
all use to open the ledger) never created its data directory first --
`Supervisor.__init__` normally does this as a side effect of a prior
`hooks run`/`hooks install`, but `belay hooks fence` can legitimately be
the first hooks command ever run for an install. Fixed by having
`_hooks_ledger_for` create the directory itself, matching what
`Supervisor.__init__` already does defensively.

## Consequences

- A human can now stop a hook session's future actions unconditionally,
  the same durable, cross-process way `belay rewind` already stops an MCP
  session's.
- No automatic fencing: nothing in the Native Agent Gate fences a session
  on its own (unlike the MCP proxy, where a real rewind always fences
  first). This is a manual operator action only, for now -- automatically
  fencing on some hook-side trigger (e.g. after `belay hooks rewind`
  restores an edit) is a real follow-up question, not resolved here.
- Does not address the remaining open R1 scope: Bash is still a static
  classifier (no `PolicyEngine`), and there is still no per-identity
  quota or anomaly-baseline tracking reaching the hook path (both are
  tightly coupled to the MCP proxy's `Plan`/`session_started`/
  `initiated_by` ledger shapes in `belay/policy/quota.py` and
  `belay/policy/baseline.py`, which hook events don't share -- wiring
  either in cleanly needs a real design decision about what "identity"
  means for a hook session, not a quick reuse).

## Testing

`tests/hooks/test_gate.py::TestLedgerEvidenceHelpers::test_session_key_matches_ledger_session_id_for_the_same_event`.
`tests/supervisor/test_session_fencing.py` (unit level against a real
`Supervisor` instance: unfenced stays normal, fenced denies Bash/file-edit/
MCP surfaces alike, a different session is unaffected).
`tests/cli/test_hooks_lifecycle.py::TestHooksFence` (real CLI, real
spawned supervisor: fence then a subsequent call in that session denies,
a different session is unaffected, fencing an already-fenced session is a
no-op not an error).
