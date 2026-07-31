# ADR 0020: Extended requirement catalog (TRUTH-*, ARCH-*, FILE-*)

## Status

Accepted (documentation correction, no behavior change).

## Context

`belay/hooks/`, `belay/supervisor/`, `belay/db/models.py`, `belay/cli/main.py`,
and their tests cite identifiers like `TRUTH-004`, `ARCH-002`, `FILE-001`
dozens of times, frequently prefixed with the word "spec" (`"spec ARCH-002"`,
`"spec TRUTH-004"`) or a specific section number (`"spec §7.1"`, `"spec
§7.2"`, `"spec §9.2 FILE-001"`, `"spec §12.1"`, `"spec §16"`, `"spec §5.2"`),
as if `docs/spec.md` (the Belay Specification 0.1) defines them.

It does not. A line-by-line check against `docs/spec.md` found:

- §7.1 is "Queue semantics" (approval item states), §7.2 is "Approver
  identity" (`approved_by` recording) -- neither discusses `HookEvent`,
  trust tiers, or a "pinned-version end-to-end bypass suite".
- §9.2 is "Evidence" (the ledger's hash-chain verification) -- it says
  nothing about file-edit capture (`FILE-001` etc).
- §12 ("Security considerations") has no numbered subsections at all --
  "§12.1" does not exist.
- §16 does not exist -- the spec ends at §14 plus three lettered appendices.
- §5.2 is "Effect types" (`create`/`update`/.../`read`) -- it says nothing
  about `T0`/`T1`/`T2` trust tiers.
- `TRUTH-*`, `ARCH-*`, and `FILE-*` are not defined anywhere in
  `docs/spec.md`, any ADR (until this one), or any other doc.
  `scripts/traceability.py` -- the CI-enforced tool that proves every real
  spec MUST has a covering test (ADR 0018) -- has no knowledge of them.

These IDs were invented while building E18/E19 (the Native Agent Gate) as a
lightweight way to number review findings and requirements, then cited as
if they were spec sections. They are real, consistently-applied engineering
invariants -- not fabricated behavior, only a fabricated *source*. This ADR
gives them one real, honest home instead of a nonexistent one, so code
comments can stop claiming spec.md says something it doesn't.

This ADR does **not** decide whether these should later become real,
RFC-2119 normative text inside `docs/spec.md` itself (which would also mean
teaching `scripts/traceability.py` about them). That's a bigger call
belonging to whoever owns the spec -- left open, tracked in project memory,
not resolved here.

## The catalog

Meanings below are transcribed from the existing code comments that
introduced each ID, not invented for this document.

### ARCH-* (Native Agent Gate supervisor architecture, E18)

| ID | Meaning | Primary site |
|---|---|---|
| ARCH-001 | The local supervisor: a persistent, authenticated per-install process (not a cold start per tool call, not a shared global daemon). | `belay/supervisor/server.py` |
| ARCH-002 | Never an unauthenticated TCP port -- a Windows named pipe or POSIX Unix domain socket only. | `belay/supervisor/server.py`, `belay/supervisor/addressing.py` |
| ARCH-003 | Installation-scoped capability: the token/approvals data are scoped to one project-anchor install, not shared across unrelated projects. | `belay/supervisor/auth.py`, `belay/supervisor/addressing.py` |
| ARCH-004 | The capability token lives outside the project directory with restrictive permissions -- never solely an environment variable (an agent can set its own env vars). | `belay/supervisor/auth.py`, `belay/supervisor/addressing.py` |
| ARCH-006 | Duplicate hook event IDs MUST be idempotent *durably* -- across a supervisor restart, not just for one process's in-memory lifetime. | `belay/supervisor/idempotency.py`, `belay/db/models.py` |
| ARCH-007 | Recovery: an abandoned pipe/socket after a hard kill, and durability of approvals/idempotency state across an unclean restart. | `tests/supervisor/test_recovery.py` |
| ARCH-008 | On-demand supervisor lifecycle (spawn if not already running); OS service-manager integration is explicitly not built yet (P1). | `belay/supervisor/lifecycle.py`, `belay/cli/main.py` |

`ARCH-005` is referenced nowhere in the codebase -- there is no ARCH-005
requirement to catalog.

### FILE-* (native file-edit capture for rewind, E18.3)

| ID | Meaning | Primary site |
|---|---|---|
| FILE-001 | Native `Edit`/`Write`/`NotebookEdit` calls are captured (pre-edit snapshot) as a side effect of being allowed, so they can be rewound later. | `belay/hooks/gate.py`, `belay/hooks/file_snapshot.py` |
| FILE-004 | A rewind's conflict check: the file's *current* content must match the recorded post-edit hash before restoring -- something touched it since is refused, not clobbered. | `belay/hooks/file_snapshot.py` |
| FILE-005 | A path that didn't exist before the edit is restored by *deleting* it, not by writing empty bytes. | `belay/hooks/file_snapshot.py`, `belay/db/models.py` |
| FILE-006 | A file exceeding the capture size cap downgrades reversibility and pauses for human approval instead of being silently allowed uncaptured. | `belay/hooks/gate.py`, `belay/hooks/file_snapshot.py` |

`FILE-002` and `FILE-008` appear only in bundled citation lists
(`FILE-001/002/004/005/006/008`) with no dedicated explanatory comment found
anywhere in the codebase; this catalog does not guess their intended
meaning rather than invent one.

### TRUTH-* (honesty requirements for trust-tier claims, E18.4/E18.7)

| ID | Meaning | Primary site |
|---|---|---|
| TRUTH-004 | A host integration may only claim `trust_tier: "T1"` (PROTECTED) after its pinned-version end-to-end bypass suite has actually been run and passed against a real installed binary -- never asserted ahead of that evidence. | `belay/hooks/claude_code_adapter.py`, `tests/hooks/test_live_conformance.py` |
| TRUTH-010 | `T1` does not claim resistance to an arbitrary process running as the same OS user as the agent -- a stated scope limit, not an oversight. | `belay/hooks/gate.py`, `belay/hooks/claude_code_adapter.py`, [`docs/security/threat-model.md`](../security/threat-model.md) |

## Consequence

Code comments across `belay/hooks/`, `belay/supervisor/`, `belay/db/models.py`,
`belay/cli/main.py`, and their tests were corrected to stop attributing
these IDs to `docs/spec.md` section numbers, and to point here instead. No
behavior changed -- this is a citation fix, not a spec change.
