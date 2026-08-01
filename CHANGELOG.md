# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.

## [Unreleased]

### Added

- **R1.6 correctness lock -- six concrete gaps closed before any further
  parallel hook-specific tracking is added on top of R1's five slices:**
  - **Fail-closed configured policy:** `Supervisor._load_contract_set`/
    `_load_quota_config`/`_load_extra_allowlist` used to collapse "never
    configured" and "configured but now broken" into the same permissive
    fallback. Once a pointer file exists (an operator opted in via `belay
    hooks install --contracts`/`--quota-max`/`--allowlist-extra`), a
    missing/unreadable/invalid target now returns a distinct
    `ConfigUnavailable` sentinel, and `_decide_pre` denies every event
    outright (`configured_policy_unavailable`) rather than silently
    reverting to unconfigured behavior.
  - **`hooks uninstall` actually cleans up:** `Manifest` gained an
    `extra_files` field recording exactly which contracts/quota/allowlist
    pointer files an install wrote; `hooks uninstall` now removes them.
    `hooks install` also self-heals a bare reinstall that omits a
    previously-given flag -- the stale pointer for that flag is deleted,
    not left to silently keep affecting a freshly (re)installed
    supervisor.
  - **Exact-match Bash allowlist entries:** a bare `--allowlist-extra`
    entry (`npm run lint`) still allows trailing arguments
    (`npm run lint --fix`) as before; a new `!`-suffixed exact-match form
    (`npm run lint!`) does not -- for entries where any extra argument
    could flip a read-only command into a mutating one.
  - **`packs/claude-code-native/contracts.yaml`:** the README's
    `hooks install --contracts` example pointed at
    `packs/filesystem/contracts.yaml`, which declares MCP-server-side tool
    names (`read_file`/`write_file`/...) for the *proxy* path -- the
    Native Agent Gate resolves native calls by their literal Claude Code
    name (`Write`/`Edit`/`NotebookEdit`), so that pack could never match
    and made every native file edit deny with `contract_missing`. The new
    pack declares the three native tool names directly.
  - **Working-tree dirty state folded into `repo_identity`:** each host
    adapter's `_repo_identity()` now also runs a cheap
    `git diff-index --quiet HEAD --` check and folds a `:dirty`/`:clean`
    suffix into the identity string `_plan_id` hashes -- an approval
    granted while the tree was clean no longer silently covers the same
    command/call after an uncommitted edit to a tracked file (known gap:
    untracked new files aren't detected by this cheap check).
  - **Single-use approval consumption:** `ApprovalQueue.consume()` claims
    an `approved` item for a specific hook `event_id`, via a real
    conditional SQL `UPDATE ... WHERE consumed_by_event_id IS NULL`
    (compare-and-swap) -- not a Python-side read-then-write, which a real
    `threading` concurrency test proved was not actually atomic under
    concurrent callers. The same hook event redelivering the identical
    dispatch stays idempotently allowed; a genuinely new event that
    happens to hash to the same `plan_id` now denies
    (`approval_already_consumed`) and must request a fresh approval,
    closing the gap where one human decision covered an unbounded number
    of separate future executions.

- **Operator-configurable extra Bash allowlist, R1 fifth slice ([ADR 0024](docs/adr/0024-r1-native-gate-configurable-allowlist.md)):**
  Bash's remaining gap (still a static classifier, no `PolicyEngine`) was
  explicitly scoped as a genuinely different problem from the
  contract/quota slices -- commands are arbitrary shell text, not a fixed
  tool name, so there's no stable identity to resolve a `Contract`
  against. What *is* a bounded slice: the hardcoded safe-read allowlist
  (`ls`, `cat`, `git status`, etc.) is now extensible. `belay hooks
  install --allowlist-extra <file>` (opt-in, off by default) parses a
  plain-text file of literal command prefixes (never regex -- an
  operator-authored regex risks an accidental hole in a security
  allowlist); an entry only ever turns a PAUSE into an ALLOW, checked
  after the built-in patterns and after the same shell-metacharacter
  guard every command already passes through, so an entry can never
  itself become a chaining/redirection/substitution bypass. This does
  **not** give Bash a `PolicyEngine` or any reversibility/blast-radius
  reasoning -- it's a configurable allowlist, nothing more; extending Bash
  to real policy-evaluated governance remains explicitly out of scope.

- **Native Agent Gate per-OS-user quota, R1 fourth slice ([ADR 0023](docs/adr/0023-r1-native-gate-quota.md)):**
  E15 gives the MCP proxy a per-identity rolling cap on approved
  irreversible actions; the Native Agent Gate had no equivalent because it
  has no `--initiated-by` identity concept and a completely different
  ledger event shape. Resolved both: identity is `HookEvent.os_user`
  (obtained from the OS itself, not agent-supplied), and a parallel
  `belay/hooks/quota.py::HookQuotaTracker` counts approved hook-gated
  actions from `hook_pre_tool_use`/`approval_resolved` events (now
  carrying `os_user`, an additive ledger change). `belay hooks install
  --quota-max <N> --quota-window <window>` (opt-in, off by default) makes
  a *new* pause-worthy action (Bash, native MCP, oversized file edit)
  hard-deny once an OS user hits the cap within the window, instead of
  being queued -- unlike E15's allow-to-pause escalation, this escalates
  pause-to-deny, since everything that needs this check already pauses by
  default in the hooks world. Persisted the same way ADR 0021's
  `--contracts` is (a small JSON pointer file the supervisor best-effort
  loads at construction).

- **Native Agent Gate session fencing, R1 third slice ([ADR 0022](docs/adr/0022-r1-native-gate-session-fencing.md)):**
  the MCP proxy fences a session before a real `belay rewind`
  (`is_fenced()` refuses new steps thereafter); the Native Agent Gate had
  no equivalent at all. `belay hooks fence <host_session_id> --host
  <host> --db <db>` now writes the same `session_fenced` ledger fact
  under the identical key `ledger_session_id`/the new shared
  `session_key()` helper compute, and `Supervisor._decide_pre` checks
  `is_fenced()` once, before any surface dispatch -- so fencing closes
  Bash, file edits, and native MCP calls uniformly. No `unfence`; start a
  new session instead. Found and fixed a real bug while testing this:
  `_hooks_ledger_for` never created its data directory first (relied on a
  prior `hooks run`/`hooks install` doing it), which `belay hooks fence`
  can legitimately be the first command to call.

- **Native Agent Gate contract check, R1 first slice ([ADR 0021](docs/adr/0021-r1-native-gate-contract-check.md)):**
  an audit of `belay/proxy/lifecycle.py` vs. `belay/hooks/gate.py` found the
  MCP proxy denies `contract_missing` for an undeclared tool while the
  Native Agent Gate allowed native `Edit`/`Write`/`NotebookEdit` calls
  unconditionally -- the same action, opposite defaults. `belay hooks
  install --contracts <file>` (opt-in, off by default -- every existing
  install is unchanged) now resolves the tool name against a real
  `ContractSet` the same way the proxy's `resolve()` does; no match is a
  hard `deny` (not queued for approval, matching spec §4.6's treatment of
  a missing contract as a configuration problem). Recorded via a new
  per-install pointer file (`SupervisorIdentity.contracts_pointer_path`)
  the supervisor best-effort loads at construction. The same
  `--contracts` file also reaches native `mcp__server__tool` calls
  (`belay/hooks/gate.py::evaluate_mcp_call`): a declared contract whose
  every effect is `type: "read"` now auto-allows without touching the
  approval queue, the same provable-safe-read case the MCP proxy already
  auto-allows via `readOnlyHint` -- this only ever narrows the
  pause-everything default, never widens it (no contract, or any
  non-read effect, still pauses exactly as before). Bash is untouched by
  this slice -- still a static classifier, no `PolicyEngine`, tracked as
  open R1 scope in
  [`docs/security/threat-model.md`](docs/security/threat-model.md).

- **Fix: `trust_tier` overclaimed `T1` for every Claude Code surface, not
  just Bash.** `claude_code_adapter._trust_tier()` applied
  `_VERIFIED_TRUST_TIER` to every event regardless of `surface`, so an
  Edit/Write or native MCP event's ledger entry would have recorded
  `trust_tier: "T1"` even though `tests/hooks/test_live_conformance.py`
  (E18.7) only ever exercises the Bash surface. Scoped to `surface ==
  "shell"` only; every other surface now honestly reports `UNKNOWN`.
  Caught while building
  [`docs/adapter-compatibility.md`](docs/adapter-compatibility.md), a
  real per-host, per-surface trust-tier matrix generated by reading the
  adapter code.

- **Spec MUST traceability matrix (docs/plan.md §8), closing a documented
  gap:** `scripts/traceability.py` hand-curates every distinct normative MUST
  extracted from `docs/spec.md` (31 requirements), scans `tests/` and
  `conformance/tests/` for `@spec("X.Y")` markers on test docstrings, and
  fails with a named MUST if coverage is incomplete. Generates
  `docs/traceability.md` (spec section -> MUST -> covering test(s)). Wired
  into CI (`.github/workflows/ci.yaml`) as a build-failing step, not a
  decorative doc. `@spec(...)` markers added across `tests/contracts/`,
  `tests/executor/`, `tests/proxy/`, `tests/planner/`, `tests/policy/`,
  `tests/approvals/`, `tests/ledger/`, `tests/rewind/`. One genuine gap found
  during extraction (policies, like contracts, MUST reject unknown fields per
  §14 -- no existing test covered it for `PolicyDoc`) and closed with a new
  test (`tests/policy/test_engine.py::test_unknown_top_level_field_in_policy_doc_is_rejected`).
  Self-tests for the generator itself in `tests/tools/test_traceability.py`.
  See [ADR 0018](docs/adr/0018-traceability-matrix.md). This closes the gap
  flagged below under "Known gaps" (2026-07-23).

- **Statistical anomaly baselines (E10, plan-v2 §"E10"):** `belay/policy/baseline.py`
  -- deterministic, no-LLM, no-network per-session rolling mean/stddev
  (Welford's algorithm) computed from the ledger's own `plan_created`
  history. New `anomaly` policy dimension in `PolicyEngine.evaluate`,
  combined with `tools`/`quiet_hours`/irreversible-default by the same
  max-severity rule. Zero manual configuration required (`min_samples=10`,
  `z_score_threshold=3.0`, `verdict=pause` by default); cold-start below
  `min_samples` never blocks. `examples/demo_anomaly.py`,
  `docs/adr/0010-e10-anomaly-baselines.md`.
- **Real SQL dry-run adapter (E11, plan-v2 §"E11"):**
  `belay/planner/adapters/sql.py` -- new `sql_simulator` plan basis (spec
  §5.3), slotted `native_dry_run > sql_simulator > dry_run > contract`.
  Runs a contract's new optional `sql` capture/effect hint
  (`belay/contracts/model.py::SqlHint`, additive -- old contracts load
  unchanged) as a real `BEGIN; ...; ROLLBACK` transaction against a real
  SQLAlchemy `Engine` to get a genuine affected-row count, never
  committing on any path (verified by a crash-mid-simulation test with no
  explicit rollback). Bind parameters reuse the existing §4.3 expression
  language, no second templating syntax. SQLite tested and working;
  Postgres implemented via the same dialect-agnostic API but not verified
  against a live instance in this sandbox (see the ADR's honesty note).
  `examples/contracts/crm.yaml` (`crm.bulk_delete`'s `sql` hint),
  `examples/demo_sql.py`, `docs/adr/0011-e11-sql-dry-run.md`.
- **Counterfactual replay (E12, plan-v2 §"E12"):**
  `belay/ledger/counterfactual.py::run_counterfactual` -- "what would have
  happened if a human had decided differently at an approval/policy point,"
  computed entirely offline from the ledger: zero real upstream calls, zero
  mutation of the real session (a `CounterfactualBranch` holds only a
  read-only tuple of `Event`s, no `LedgerStore` handle at all). Reuses
  `belay.ledger.replay.replay()` for the real session's baseline final
  state and the existing `Basis` literal (E4/E11) for divergent steps'
  estimates, rather than duplicating either. Honesty rule (mirrors E7's
  `fully_rewound`): steps identical to reality are `unchanged` (the real
  recorded result); steps that diverge *because of* the override with a
  safe read-only estimate available are `diverged` with that `Basis` (or
  the branch's own `"simulated"` marker); steps with no safe way to
  re-derive an outcome are `unknown`, never fabricated. `belay
  counterfactual <session_id> --at-step <n> --override '<json>' [--json]`.
  `examples/demo_counterfactual.py`,
  `docs/adr/0012-e12-counterfactual-replay.md`.
- **Cryptographically signed, offline-verifiable evidence (E13, plan-v2
  §"E13"):** `belay/ledger/signing.py` -- Ed25519 (`cryptography`, no
  hand-rolled crypto) `SigningKey` persisted to an operator-controlled file
  (never inside the SQLite ledger). `sign_session` reuses `verify_chain`
  (E2) for the chain's terminal hash and `belay/canonical.py` for the
  signed summary, never a second chain-recomputation or canonicalization.
  `verify_evidence` is a pure function needing only the exported bundle:
  reports the *precise* failing stage (`chain` / `coherence` / `signature`
  / `summary_mismatch`), matching `verify_chain`'s existing per-index
  precision instead of an opaque pass/fail. New CLI: `belay keygen`,
  `belay verify-export <session_id> --key <path> -o <file>`, `belay
  verify-evidence <file> [--pubkey <path>]` -- the last needs zero database
  access, zero network, tested in a directory with no `belay.db` present at
  all. Covers all four tamper scenarios (payload byte flipped, re-signed
  with a different key, summary fields edited without re-signing, events
  appended after signing) plus a Hypothesis property test that any
  single-byte flip in the embedded events always fails verification.
  `belay verify` (E2's unsigned path) is completely unaffected.
  `examples/demo_signed_evidence.py`,
  `docs/adr/0013-e13-signed-evidence.md`.
- **Identity attribution: who told the agent to do this (E14, plan-v2
  §"E14"):** `initiated_by`/`on_behalf_of` promoted to named, typed
  `belay/ledger/model.py::Event` fields (`initiated_by` required-in-spirit,
  `on_behalf_of` optional) -- an externally-asserted identity Belay trusts
  from its deployment's own front door, never a login system Belay builds
  itself (scope boundary, see the ADR). `Lifecycle.start_session` now
  requires `initiated_by` (an accidental omission is a loud `TypeError`,
  never a silently-blank session); bound once on `session_started` rather
  than repeated per event, surfaced session-wide via `belay/ledger/replay.py`'s
  `SessionState`. New CLI: `belay wrap`/`belay run --initiated-by
  <identity> [--on-behalf-of <identity>]`; `belay verify`/`belay
  verify-evidence` (E13) both surface `initiated_by`/`on_behalf_of` in their
  reports. E13 integration: `sign_session`'s signed summary now covers
  `initiated_by`/`on_behalf_of`, so tampering with who initiated a session
  is detected exactly like tampering with the chain/`event_count`
  (`SignedEvidence` also tightened to `extra="forbid"`, closing a gap the
  new Hypothesis property test found: silently renaming a bundle field used
  to fall back to its default instead of failing to parse). Regression: all
  7 pre-existing `start_session()` call sites across E3-E13 test/conformance
  fixtures updated to pass an explicit `"test-fixture"` identity -- no test
  was left silently broken by the new required parameter.
  `examples/demo_attribution.py`,
  `docs/adr/0014-e14-identity-attribution.md`.
- **Per-identity irreversible-action quota (E15, plan-v2 §"E15"):**
  `belay/policy/quota.py::QuotaTracker` -- a rolling-window count of one
  E14 `initiated_by` identity's approved-and-executed irreversible actions,
  read from the ledger across all of that identity's sessions (same
  "read the ledger, no parallel store" philosophy as E10's
  `BaselineStore`). New `quota` policy dimension in `PolicyEngine.evaluate`,
  combined with `tools`/`quiet_hours`/`anomaly`/irreversible-default by the
  same max-severity rule -- composes with, does not replace, E4's per-call
  `Cap`. Only actions that were actually approved (or auto-allowed) *and*
  executed count (`step_committed` proof); denied or still-pending actions
  never do. `Defaults.quota` (`QuotaDefaults`) ships `enabled=False` by
  default -- unlike E10's statistically-derived zero-config baseline, a
  quota number is an operator's own risk judgment call, not something
  Belay can derive from data alone (honest caveat documented in the ADR).
  `reasons` cite the identity, current count, window, and configured max.
  `examples/demo_quota.py`, `docs/adr/0015-e15-identity-quota.md`.
- **Blast-radius self-explanation returned to the agent (E16, plan-v2
  §"E16"):** `belay/policy/explain.py::explain(policy_result, plan,
  contract=None) -> Explanation` -- a pure formatting function, no new
  computation: every number in its output is traceable back to the real
  `PolicyResult.reasons` already computed by `PolicyEngine.evaluate`
  (caps/tools/quiet_hours/irreversible-default from E4, `anomaly` from E10,
  `quota` from E15). `Explanation` (`verdict`, `headline`, `dimensions`,
  `suggested_action`) now rides on every governed response the calling
  agent receives, not just what a human sees via `belay approvals
  list`/the ledger: `pending_approval` carries it inline,
  `policy_denied`/`approval_rejected`/`approval_expired` carry it in
  `BelayError.detail["explanation"]`, and `allow` responses get a minimal
  empty-dimensions `Explanation` too (for symmetry), folded additively into
  `CallToolResult.structuredContent` in `belay/proxy/server.py` without
  touching the upstream's own response shape. `suggested_action` is a
  deterministic, mechanical suggestion (never guessed) present only when a
  contract declares a `$args.<path>`-referencing narrowing argument via
  `conditions` (conditional contracts) or `sql.params` (E11) -- absent
  otherwise. Disclosure policy: full transparency, applied uniformly across
  every dimension (documented and justified against spec §12 in the ADR).
  Hypothesis property test confirms `explain()` never raises and never
  references a number absent from `reasons`, across real `PolicyResult`s
  from the real `PolicyEngine.evaluate()`. `examples/demo_self_explain.py`
  (a scripted agent that reads its own pause's `suggested_action`, narrows,
  resubmits, and gets `allow` -- zero human approval steps),
  `docs/adr/0016-e16-blast-radius-self-explanation.md`.

- **Safe installer lifecycle (E17, plan.md §8):** `belay init --client
  claude-desktop|claude-code|cursor|codex|opencode|auto|all` registers
  Belay in a client's own MCP config in one command, non-destructively
  merged alongside whatever else is already configured. `belay
  uninstall`/`belay doctor` read a `.belay-manifest.json` written alongside
  each config as the one source of truth for whether the file changed
  since install, instead of guessing from content. E17.1 hardening (found
  by review, not shipped broken): re-running `init` no longer overwrites
  the pre-install backup with already-belay content; `uninstall` uses the
  name recorded in the manifest, not a CLI default; a dry-run preview and
  the real write can no longer diverge; a config uninstalled back to a
  state that never existed is deleted, not left an empty stub; a failed
  manifest write rolls the config back instead of leaving belay
  installed-but-unmanaged; `doctor` reports **BROKEN** when the manifest
  exists but the entry doesn't. Also lands the **spec MUST traceability
  matrix** (`scripts/traceability.py`, see the entry above and
  [ADR 0018](docs/adr/0018-traceability-matrix.md)) as part of this
  entrega's DX work.

- **Native Agent Gate: `belay hooks install` (E18):** a deterministic,
  no-LLM gate for an agent's *native* tool calls (Bash, file edits, and
  native MCP calls made outside `belay run`'s own proxy) -- first slice,
  **Claude Code only**. Every Bash command runs through
  `belay/hooks/decision.py`: a narrow allowlist of read-only commands is
  allowed, everything else pauses (including an allowlisted-looking
  command combined with shell chaining/redirection/substitution, rejected
  before the allowlist is even checked). Native `Edit`/`Write`/
  `NotebookEdit` calls are allowed by default and captured for rewind
  (`belay hooks rewind <event_id>`/`list-edits`) -- gating every routine
  edit would make the tool unusable for real coding. Native
  `mcp__server__tool` calls always pause, unconditionally, since they
  never pass through Belay's own contract-enforcing proxy. All decisions
  are made by a persistent, authenticated **local supervisor**
  (`belay/supervisor/`) over a Windows named pipe or POSIX Unix domain
  socket (never an unauthenticated TCP port), fails closed if unreachable,
  durably idempotent across a restart, with the capability token and
  approvals database both stored outside the project directory. E18.1
  hardening closed 8 P0s found in independent review (JSON wire format
  instead of pickle, private off-project approvals storage, durable
  idempotency, full-context approval binding, belay-internal-path
  protection, honest `trust_tier`, Slowloris resistance, hard-kill
  recovery). E18.2 records `PostToolUse` results into the same
  hash-chained ledger the MCP path uses. E18.4: `belay doctor` flags other
  MCP servers configured alongside belay as an ungated bypass route.
  E18.5/E18.6 add Codex and OpenCode host adapters, normalize/render only
  (verified against the real installed binaries, not wired to a live
  session -- said plainly, not oversold); Cursor has no adapter (no way
  found to headlessly verify its real hook payload shape). E18.7:
  `tests/hooks/test_live_conformance.py` is a real, opt-in, pinned-version
  end-to-end suite against the actual installed `claude` CLI --
  `claude_code_adapter._VERIFIED_TRUST_TIER` is `"T1"` for Claude Code's
  Bash surface specifically because this suite exists and passed, not
  asserted ahead of the evidence. See
  [ADR 0020](docs/adr/0020-extended-requirement-catalog.md) for the
  `TRUTH-*`/`ARCH-*`/`FILE-*` invariant IDs this entrega introduced (E17-E19
  have no dedicated ADR of their own).

- **One-command lifecycle and exclusive routing (E19):** E19.1 `belay
  detect`/`belay init --client auto` register only clients actually
  installed on this machine (real binary-presence + version detection,
  not a blind registration attempt). E19.2 `belay disable-bypass` removes
  one named non-belay MCP server entry from a client config (the write
  half of E18.4's bypass detection). E19.3 `belay hooks doctor --deep`
  verifies the registered interpreter can actually import belay and the
  supervisor is genuinely reachable, not just that the config file hash
  matches. E19.4 `belay repair` detects every belay-managed registration
  gone BROKEN across every client and hooks, and restores all of them in
  one command. E19.5 standalone `belay`(`.exe`) binaries via PyInstaller
  (`scripts/build_binary.py`), built and smoke-tested on real Linux/macOS/
  Windows CI runners (~500 MB, heavy transitive deps -- shrinking it is
  real follow-up work). E19.6 `belay release sign`/`verify`: Ed25519
  authenticity signing for a release bundle -- explicitly not OS-level
  code-signing/notarization. E19.7: the `cross-platform-clean-room` CI job
  runs the fast suite on real ubuntu/macos/windows GitHub Actions runners
  on every push; turning it on immediately found 4 genuine POSIX bugs the
  Windows-only dev environment had never exercised (missing listen-address
  directory, `AF_UNIX path too long` on macOS, a stale socket file
  blocking respawn after a hard kill, and a non-executable `install.sh`
  committed without its POSIX exec bit).

- **Verified action packs (E20):** `packs/filesystem/` and `packs/git/` --
  real, tested `Contract` sets for the actual official
  `@modelcontextprotocol/server-filesystem` (npm) and `mcp-server-git`
  (PyPI) servers, not an illustrative example. Hand-corrected past `belay
  draft-contracts`'s naive heuristic (`create_directory` fixed from a
  nonsensical draft undo to honestly `irreversible`; `write_file` is
  `conditional`). `tests/packs/test_filesystem_pack.py`/
  `test_git_pack.py` run a real multi-step saga against the real server
  with a real injected mid-saga failure. Both packs declare
  `trust_state: unverified` (the signed-registry/packaging infrastructure
  doesn't exist yet, not a doubt about correctness). See
  [ADR 0019](docs/adr/0019-e20-verified-packs-scope.md).

### Adoption/DX (not spec-numbered -- onboarding, not lifecycle)

- **`belay wrap --command/--arg`** launches any stdio MCP server, not just
  `python server.py` (found and fixed while wrapping the real
  `@modelcontextprotocol/server-filesystem`).
- **`belay draft-contracts`** proposes a starting contract per upstream
  tool from its live MCP schema/annotations (`readOnlyHint`/
  `destructiveHint`, no LLM); every draft is `provenance.verified: false`.
- **`belay dashboard`** renders a static HTML snapshot of a ledger's
  sessions/steps/approvals.
- **`belay approvals list --triage`** sorts the pending queue highest-risk
  first by a deterministic reason -- never approves or rejects anything
  itself.
- **Intent contracts** (`belay run --intent-contract <file>`): mechanically
  enforced `allowed_scope`/`forbidden_scope`/`forbidden_tools`/
  `budgets.files_changed`, hash-pinned into `session_started` from the
  moment a session begins so `belay export-pr` can label a PR's "what was
  asked" as verified, unverified, or not recorded.
- **`belay verify-test --runner pytest|jest|go`** independently runs a
  step's declared `_belay_test_ref` (argv list, `shell=False` -- an
  earlier shell-string version was injectable and is now covered by
  regression tests) instead of trusting the agent's own claim that a test
  passed.
- **`belay causal <session>`** assembles a requirement -> decision -> test
  -> undo graph straight from the ledger (`--format mermaid` for a real
  flowchart).
- **`belay rewind --intent/--keep`** undoes exactly one agent-tagged
  subgoal (`_belay_intent`) while keeping another, refusing outright
  (`rewind_intent_not_suffix`) rather than guessing when the tagged steps
  aren't a safe contiguous trailing run.
- **`belay learn <approval_id>`** compiles a human's rejection into a
  durable, mechanical `IntentContract` rule (forbid the tool, or forbid
  its file scope) -- nothing written until `--apply` says so explicitly.
- **`belay explore <session_id>...`** compares already-run session
  variants side by side (steps, files touched, proven-by-test-or-not,
  irreversible/indeterminate step count) -- a table, not an LLM verdict.
- **`belay export-pr`** packages a committed session's file changes as a
  real git branch + commit with signed evidence attached, and (with
  `--intent-contract`/`--config`) a proof-carrying PR body answering what
  was asked, what changed unasked, what was verified, and how it's undone.
- **`belay replay`** re-executes a real session against the live upstream
  with one step's args overridden, through the real governed lifecycle
  (pausing honestly on approval instead of skipping it).

## [0.1.0] - 2026-07-22

First feature-complete release: an MCP proxy giving agents contract-based,
policy-gated, reversible tool execution, L3 conformant against
`docs/spec.md` (Belay Specification 0.1). Built entrega-by-entrega (E0-E9)
per `docs/plan.md`; see `docs/adr/` for the decision record of each.

### Added

- **Scaffolding (E0):** package layout, `pyproject.toml`, ruff/mypy/pytest
  configuration, pre-commit hooks, GitHub Actions CI, Alembic migrations.
- **Contracts + expression language (E1, spec §4):** `belay/contracts` —
  `parse`/`evaluate` for the closed-grammar expression language (no
  `eval`/`exec`), YAML/JSON contract loading with JSON-Schema validation,
  canonical JSON + `set_hash`.
- **Event ledger (E2, spec §9):** `belay/ledger` — append-only, hash-chained
  events, chain + coherence verification, deterministic replay, secret
  redaction. `belay verify`.
- **L1 MCP proxy + CLI (E3, spec §3, §4.6, App. C):** `belay/proxy`,
  `belay wrap` / `belay run`. Contract resolution, the default rule for
  tools without a contract, passthrough execution, full ledger recording
  over stdio against any standard MCP client.
- **Planner + policy engine (E4, spec §5, §6):** `belay/planner`,
  `belay/policy` — dry-run effect estimation (`contract` and
  `native_dry_run` adapters), blast-radius caps, `deny > pause > allow`
  verdicts, plan expiration. `belay plan`.
- **Approvals (E5, spec §7):** `belay/approvals` — pending/approved/
  rejected/expired lifecycle, structural no-self-approval (no agent-facing
  approval surface), approver binding to `plan_id`. `belay approvals
  list|approve|reject`.
- **Saga executor (E6, spec §8):** `belay/executor` — the normative
  journaled/capturing/calling/result_recorded/compensation_registered/
  committed step cycle, idempotency keys, crash recovery from the ledger
  alone, conditional-undo re-checking.
- **Rewind (E7, spec §10):** `belay/rewind` — reverse-order compensation,
  session fencing across processes, honest `fully_rewound` reporting,
  `--dry-run` and `--skip-and-continue`. `belay rewind`. Closes L3
  conformance.
- **Public conformance suite (E8, spec §13):** `belay-conformance` — a
  target-agnostic pytest suite (`@conformance(level=...)`) driven by a
  ~6-method `ConformanceTarget` adapter, plus example contract packs
  (filesystem, CRM, email/irreversible).
- **Demo, docs, and portfolio polish (E9):** `examples/demo.py` (real,
  runnable reproduction of the `docs/plan.md` §10 scenario, `--oops`
  variant included), `docs/architecture.md` (full Mermaid component +
  lifecycle diagram), README badges/quickstart/comparison section,
  `CONTRIBUTING.md` + issue templates, `.github/workflows/release.yaml`
  (PyPI trusted publishing on tag push).

### Known gaps (tracked, not blocking v0.1.0)

- `belay approvals approve --narrow <filter>` is not implemented as CLI
  surface; the tested equivalent is re-planning with narrower args (spec
  §12, new `plan_id`) and approving that plan instead. See
  [ADR 0007](docs/adr/0007-e7-rewind.md), [ADR 0009](docs/adr/0009-e9-demo-docs-polish.md).
- `docs/traceability.md` (the spec-section -> test generator described in
  `docs/plan.md` §8) was not built in any entrega; spec MUST coverage is
  currently verified by reading the test suite, not a generated table.
  **RESOLVED 2026-07-23:** see the traceability entrega above and
  [ADR 0018](docs/adr/0018-traceability-matrix.md) -- `docs/traceability.md`
  now exists, is generated by `scripts/traceability.py`, and is checked in
  CI.
- No PyPI release exists yet; publishing requires the maintainer to
  configure trusted publishing on PyPI first, then push the `v0.1.0` tag.
- No demo GIF/asciinema recording is embedded in the README; `asciinema`
  and `vhs` were not available in the sandbox this entrega was built in. A
  VHS tape script (`examples/demo.tape`) is checked in for the maintainer
  to render.
- The default `pytest` run (fast loop, `slow`-marked subprocess/integration
  tests deselected) finishes in ~25-30s but covers `belay/` at ~89%, just
  under the §0 90% bar; the full suite (CI's second `pytest` step, `-m ""`)
  covers ~93% but takes ~85-90s, over the §0 60s bar. The two §0 criteria
  are in tension as specified; CI runs both the fast loop and the full
  suite so neither speed nor coverage is silently dropped, but no single
  `pytest` invocation satisfies both numbers at once.
