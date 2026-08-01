# ADR 0021: Native Agent Gate contract check (R1, first slices)

## Status

Accepted, implemented (opt-in, off by default).

## Context

An audit comparing `belay/proxy/lifecycle.py` (the MCP proxy's governance
engine) against `belay/hooks/gate.py` (the Native Agent Gate) found they
are not the same governance model wearing two hats -- they are
structurally different, and the single starkest divergence was this:

- MCP proxy: a tool call with no declared `Contract` and no `readOnlyHint`
  is refused with `contract_missing` (spec §4.6's default rule), unless
  the operator explicitly configured `unsafe_passthrough` for that tool.
- Native Agent Gate: a native `Edit`/`Write`/`NotebookEdit` call with no
  contract concept at all was **allowed by default**, every time,
  unconditionally.

Writing a file is writing a file. The proxy path treats an undeclared one
as a configuration problem worth refusing; the hook path treated the
identical action as safe-by-default. That is the highest-consequence gap
this project's own audit found (see project memory / the earlier
"lifecycle.py vs gate.py" review), and it is the one gap closeable without
first redesigning the whole Native Agent Gate.

This is explicitly **R1's first slice**, not R1 itself. Unifying the two
engines fully (a canonical `ActionEnvelope`, one `TransactionEngine`,
policy/quota/anomaly/intent-contract enforcement reaching native calls) is
real, multi-week engineering with its own design questions this ADR does
not resolve.

## Decision

`belay hooks install` gets a new, optional `--contracts <file>` flag.

- **Omitted (default):** zero behavior change. Every existing install of
  `belay hooks install` keeps today's allow-by-default for native file
  edits, exactly as before this ADR.
- **Provided:** the file is loaded and validated as a real `ContractSet`
  (the same loader `belay wrap --contracts` uses) at install time --
  invalid contracts fail the install immediately, nothing is written. Its
  resolved path is recorded in a new per-install pointer file
  (`SupervisorIdentity.contracts_pointer_path`, under `belay_home()`,
  same private-storage rule as the capability token and approvals DB).
  The supervisor loads it once at construction (best-effort: a missing or
  broken pointed-to file falls back to `None`/no-check rather than
  crashing the supervisor or fail-closed denying everything -- this is
  opt-in extra strictness, not a security invariant the process must
  refuse to start without).
- `belay/hooks/gate.py::evaluate_file_edit` gained a `contract_set:
  ContractSet | None = None` keyword parameter. When set, the event's
  `tool_name` (e.g. `"Write"`) is resolved against it the same way
  `belay/proxy/lifecycle.py::resolve()` resolves an MCP tool name. No
  match is a hard `deny` with `contract_missing` in the reason -- **not**
  queued for approval, matching the MCP proxy's treatment of
  `contract_missing` as a configuration problem for the operator to fix
  (declare a contract, or don't configure `--contracts` at all), not a
  one-off a human approves ad hoc. A match falls through to the existing
  capture-and-allow behavior unchanged -- this slice does not yet
  reinterpret a *present* contract's `reversibility`/`effects` for native
  edits (real follow-up work).

An operator declares a contract for a native tool the same way they'd
declare one for any MCP tool, keyed by the literal host tool name:

```yaml
belay_contract: "0.1"
tool: Write
reversibility: irreversible
effects:
  - type: update
    resource: native.file
```

### Second slice: native MCP calls narrow, never widen, the default pause

The same `--contracts` file also reaches `belay/hooks/gate.py::evaluate_mcp_call`.
Unlike the file-edit case, native MCP calls' existing default (pause,
unconditionally) is already the *safe* direction -- there is no unsafe
default to fix here. The opportunity is accuracy, not safety: when the
exact `mcp__server__tool` identity resolves to a declared contract whose
every effect is `type: "read"`, it is auto-allowed without ever touching
the approval queue -- the same provable-safe-read case
`belay/proxy/lifecycle.py::resolve()` already auto-allows via
`readOnlyHint`. Anything else (no contract, or a contract with any
non-read effect) still pauses exactly as before this slice existed --
`contract_set` only ever narrows the pause-everything default, it never
turns a pause into an allow without positive, declared evidence.

```yaml
belay_contract: "0.1"
tool: mcp__github__list_issues
reversibility: irreversible
effects:
  - type: read
    resource: github.issues
```

## Consequences

- Closes the single worst-audited divergence (file edits), opt-in, with
  zero risk to any existing install that doesn't pass `--contracts`.
- Native file edits can now be made to share the MCP proxy's own
  `contract_missing` honesty, at the cost of the operator maintaining a
  second, small contract file for host tool names.
- Native MCP calls gain a narrow, evidence-only auto-allow for provably
  read-only tools, without weakening the pause-everything default for
  anything not explicitly declared read-only.
- Does **not** touch Bash (still a static classifier, no `PolicyEngine`)
  -- remains open R1 work.
- Does **not** wire quotas, anomaly baselines, intent-contract
  enforcement, or session fencing into the hook path -- still absent,
  still tracked as open R1 scope.
- `docs/security/threat-model.md`'s "MCP proxy vs. Native Agent Gate:
  known divergence" section is updated to reflect these two closed gaps
  while the rest of the divergence stands.

## Testing

`tests/hooks/test_gate.py::TestFileEditContractCheck` (unit level, four
cases: no contract_set configured stays unchanged, a configured set with
no matching contract denies before ever capturing a snapshot, a matching
contract falls through to allow, and contract_missing takes priority over
an unrelated "no path argument" error) and `TestMcpCallContractCheck`
(five cases: unchanged default, all-read contract auto-allows without
touching the queue, a contract with a non-read effect still pauses, no
matching contract still pauses, and a server literally named "belay" gets
no free pass). `tests/supervisor/test_contract_set_loading.py` (the
pointer-file load path: absent, valid, missing target, invalid content,
empty). `tests/cli/test_hooks_lifecycle.py::TestInstallWithContracts`,
`TestFileEditContractCheckEndToEnd`, and
`TestMcpCallContractCheckEndToEnd` (the real CLI, install-time validation
failures, and genuine spawned-supervisor round trips proving the pointer
file written at install time is actually read by the process that later
spawns).
