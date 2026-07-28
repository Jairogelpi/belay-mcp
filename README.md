# Belay

[![CI](https://github.com/Jairogelpi/belay-mcp/actions/workflows/ci.yaml/badge.svg)](https://github.com/Jairogelpi/belay-mcp/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Conformance: L3](https://img.shields.io/badge/conformance-L3-brightgreen.svg)](conformance)

> PyPI badge intentionally omitted: `belay-mcp` is not published yet (see
> "Release status" below) — a badge pointing at a nonexistent PyPI project
> would 404, so it's left out rather than faked.

**Safe, reversible tool execution for AI agents.**

Belay is an MCP proxy that sits between an agent and its tool servers. It
turns "the agent can call anything" into "every tool call is declared,
previewable, gated, and — when it goes wrong — reversible."

> Status: **`v0.1.0` tagged, L3 conformance.** E0-E9 (`docs/plan.md`) shipped
> the full lifecycle — contracts (§4), ledger (§9), the L1 proxy (§3, §4.6,
> Appendix C), planner + policy (§5, §6), approvals (§7), the saga executor
> (§8), and rewind (§10), diagrammed in
> [`docs/architecture.md`](docs/architecture.md). Nine further entregas
> (E10-E18, `docs/plan-v2.md`) shipped past v0.1.0 without breaking L3 — see
> "What's new since v0.1.0" below (E18 is a first slice — Claude Code only,
> said plainly in its own section below). 613 tests,
> [`docs/traceability.md`](docs/traceability.md) proving every normative MUST
> in the spec has a real test (CI-enforced, not a stale doc). The protocol is
> specified in [`docs/spec.md`](docs/spec.md) (Belay Specification 0.1).
> **Coverage: ~81% repo-wide** (`fail_under = 79`, CI-enforced floor against
> regressions — raised as more lands, never lowered silently). The
> spec-normative core stays high where it matters — `contracts/` 92-100%,
> `policy/` 88-100%, `ledger/` 93-100%, `rewind/` 87-94%, `intent/` (scope
> enforcement) 79-100% — it's the newer adoption/DX modules (dashboard,
> explore, export-pr's git plumbing) pulling the global number down, not
> the safety-critical path.

## Why

Agents that can delete, spend, or send are one bad plan away from an
incident. Belay's answer isn't "trust the model more" — it's infrastructure:

- **Contracts** (spec §4) declare, per tool, whether an action is
  `reversible`, `irreversible`, or `conditional`, and — if reversible — what
  the concrete undo call is.
- **Dry-run planning** (spec §5) predicts effects (`create`, `update`,
  `delete`, `send`, `spend`, ...) before anything executes, honestly marking
  what's estimated versus exact.
- **Policy** (spec §6) enforces blast-radius caps (row counts, spend limits,
  recipient counts, quiet hours) and picks the most restrictive verdict.
- **Human approval** (spec §7) parks anything the policy pauses, with
  no-self-approval enforced structurally — an agent cannot approve its own
  action through any surface Belay exposes.
- **Saga execution** (spec §8) commits actions as staged steps and
  materializes each compensation at commit time, so undo never re-evaluates
  live state.
- **Rewind** (spec §10) runs compensations in reverse order and reports
  honestly what was undone, what's irreversible, and what's indeterminate —
  it never claims "fully rewound" unless that's true.
- **An append-only, hash-chained ledger** (spec §9) makes every decision and
  every tool call independently verifiable and replayable.

No LLM sits on the safety path. Belay is deterministic end to end.

## What's new since v0.1.0

Seven entregas landed on top of the v0.1.0 lifecycle (`docs/plan-v2.md`,
ADRs 0010-0018), each additive — none weakened an existing test or broke L3
conformance:

- **Statistical anomaly baselines** (E10) — a per-session rolling
  mean/stddev (Welford's algorithm, no LLM, no manual threshold) pauses an
  action that's wildly outside its own session's normal pattern, even with
  zero `Cap` configured for that tool. `examples/demo_anomaly.py`.
- **Real SQL dry-run** (E11) — instead of a declared estimate, a
  `BEGIN ... ROLLBACK` against the actual database reports the real
  affected-row count before a human approves anything, never committing.
  `examples/demo_sql.py`.
- **Counterfactual replay** (E12) — `belay counterfactual <session> --at-step
  N --override '{"verdict":"deny"}'` answers "what would have happened if a
  human had decided differently here" entirely offline, from the ledger
  alone — never calling the real upstream, never touching the real
  session's chain. Honest by construction: it only ever reports
  `unchanged`, `diverged` (with the real basis), or `unknown` — never a
  fabricated concrete outcome. `examples/demo_counterfactual.py`.
- **Signed, offline-verifiable evidence** (E13) — `belay verify-export` +
  `belay verify-evidence` produce and check an Ed25519-signed bundle that
  needs nothing but the file itself and a public key: no `belay.db`, no
  network. Tamper detection is precise (chain vs. signature vs. summary
  mismatch), not a single opaque pass/fail. `examples/demo_signed_evidence.py`.
- **Identity attribution** (E14) — every session is bound to an explicit
  `--initiated-by` (and optional `--on-behalf-of`) identity, folded into
  E13's signature so forging *who* triggered a session is caught exactly
  like tampering with the ledger. `examples/demo_attribution.py`.
- **Per-identity irreversible-action quota** (E15) — beyond E4's per-call
  caps, a rolling window limits how many irreversible actions one identity
  can accumulate, so "I approved this once" can't silently become "the
  agent did it 200 times." `examples/demo_quota.py`.
- **Blast-radius self-explanation** (E16) — the governed response back to
  the *agent itself* (not just the human's CLI) carries a structured,
  template-filled explanation of why a call paused/was denied, with a
  deterministic `suggested_action` when one mechanically applies. In
  `examples/demo_self_explain.py` the agent reads its own explanation,
  narrows its request, and gets `allow` — with zero human approval step.

### Adoption/DX (not spec-numbered — onboarding, not lifecycle)

- **Any stdio MCP server, not just Python** — `belay wrap --command/--arg`
  launches anything (`npx`, a compiled binary, ...); previously hardcoded to
  `python server.py`. Found and fixed while wrapping the real
  `@modelcontextprotocol/server-filesystem`.
- **Fixed: strict `outputSchema` upstreams** — the self-explain payload
  (E16) moved from `structuredContent` to MCP's `meta` field; putting it in
  `structuredContent` broke any real upstream whose tool declares
  `additionalProperties: false`, which the example servers never did.
- **`belay init --client ...`** registers Belay in Claude Desktop/Code/
  Cursor/Codex/OpenCode's MCP config in one command (E17), merged
  non-destructively alongside whatever other MCP servers are already
  configured — see "Registering with an MCP client" below.
  **`belay uninstall`/`belay doctor`** (E17) use a `.belay-manifest.json`
  written alongside each config as the one source of truth for whether the
  file changed since install, rather than guessing from content. E17.1
  hardened the lifecycle after review found it wasn't reinstall-safe:
  running `init` twice no longer overwrites the pre-install backup with
  already-belay content (so `uninstall` still restores the *true* original,
  byte-for-byte, atomically); `uninstall` uses the name actually recorded in
  the manifest, not a CLI default, so `init --name foo` is always fully
  removable; `init`'s dry-run preview and its real write can no longer
  diverge (a config changed in that window aborts the write instead of being
  silently clobbered); a config uninstalled back to a state that never
  existed before install is deleted, not left as an empty stub; a failed
  manifest write rolls the config back instead of leaving belay
  installed-but-unmanaged; and `doctor` reports **BROKEN** rather than
  "registered" when the manifest exists but the entry itself doesn't.
- **`belay draft-contracts`** proposes a starting contract per upstream tool
  from its live MCP schema/annotations — see "Drafting contracts from a
  live server" below.
- **`belay dashboard`** renders a static HTML snapshot of a ledger's
  sessions/steps/approvals — see "Dashboard" below.
- **`belay approvals list --triage`** sorts the pending queue highest-risk
  first with a deterministic reason (reversibility, plan confidence,
  unknown effects, how many policy dimensions fired) — a pure label/sort
  over data the plan/policy stages already computed, no LLM, no new
  decision authority. It never approves or rejects anything; that stays
  CLI-only and human-typed (spec §12 no-self-approval) — this just cuts
  through a long queue to what actually needs eyes first.
- **Intent contract** (`belay run --intent-contract <file>`) turns a task's
  brief into an execution boundary, not just documentation. Deliberately
  narrower than "no refactoring auth" as free English -- only what's
  mechanically checkable is enforced: `allowed_scope`/`forbidden_scope`
  (glob patterns against a call's `path`), `forbidden_tools` (exact
  denylist), and `budgets.files_changed` (a cap on distinct paths touched).
  A violating call is denied (`policy_denied`) before it ever reaches the
  upstream. `acceptance` criteria stay plain informational text for a human
  (or `belay export-pr`'s PR body) to judge — claiming to machine-verify
  "the public API wasn't touched" without real static analysis would be
  worse than not checking it. See `examples/contracts/intent-timezone.yaml`.
  Its canonical hash (`belay/canonical.py`, same mechanism as contracts'
  `set_hash`) is folded into `session_started`'s payload the moment the
  session begins — part of the hash chain and the signed evidence bundle
  (E13) from the start, not a fact asserted after the fact. `belay
  export-pr --intent-contract` compares the file it's given against that
  recorded hash and labels its "What was asked?" section: **verified**
  (hashes match), **⚠ UNVERIFIED** (flagged loudly, never silently
  trusted), or **not verified** (the session recorded no hash at all).

```bash
belay run --config belay.wrap.json --intent-contract intent.yaml
# agent tries: fs.write_file(path="src/auth/login.py", ...)
# -> denied before the upstream ever sees it:
# {"code": "policy_denied", "detail": {"reason": "intent_contract:forbidden_scope", ...}}
```
- **`belay verify-test`** closes the gap in `_belay_test_ref` (a bare
  string the agent itself supplies, never independently checked —
  `tests/fake.py::test_ok` would show up as "proven" whether or not it
  exists or passes). `--runner pytest|jest|go` reads the step's own
  `_belay_test_ref` from the ledger and builds the command mechanically
  from a fixed template — **not** typed in free-form, so an agent or
  operator cannot substitute an easier-to-pass command under a false test
  label (`_belay_test_ref="tests/fake.py::test_ok"` plus a `--cmd` that
  merely happens to exit 0 is caught: it's recorded as `mode: "command"`
  and never shown as a verified *test*). Real git context (HEAD, tree
  hash, dirty flag) and who ran it are recorded too. `belay causal`/`belay
  export-pr` distinguish three tiers per step: **VERIFIED** (`--runner`
  ran the step's own declared test, exit 0), **claimed, never run** (a
  `_belay_test_ref` label with no matching verification), or **test
  FAILED** (ran, non-zero exit) — never a single "proven: yes/no" that
  trusts the agent's word, and never confusing "some command passed" with
  "this specific test passed." The runner's command is built as an **argv
  list executed with `shell=False`** — `test_ref` (agent-supplied,
  untrusted) is passed as a single argument, never interpolated into or
  parsed as a shell string, so `; rm -rf`, `&& true`, backticks, `$()`, or
  a newline inside it have no shell to be interpreted by (an earlier
  version built a shell string and was exploitable this way — caught in
  review, fixed, and covered by injection tests before this shipped). A
  step verified while the tree was dirty is labeled **VERIFIED ON DIRTY
  TREE**, not a plain VERIFIED — the recorded `tree_hash` reflects HEAD,
  not the modified working tree, so it isn't fully reproducible from that
  hash alone.

```bash
belay verify-test s_abc123 --step 1 --runner pytest
# -> running declared test: 'tests/test_auth.py::test_login' via pytest
# -> TEST PASSED (exit_code=0, 340ms, output_hash=sha256:..., git_head=...)
belay causal s_abc123
#   step 1: fs.write_file (auth.py)
#     VERIFIED by test: tests/test_auth.py::test_login (exit_code=0, output_hash=sha256:...)
```
- **`belay causal <session>`** answers "what requirement caused this, what
  did it read before deciding, what test proves it was necessary, what
  would undo it" straight from the ledger — not a new subsystem, just
  everything already recorded (`state_captured`, `plan_created`'s
  `intent_id`/new `test_ref` tag, `policy_evaluated`,
  `compensation_registered`) assembled into one graph instead of left for
  a human to grep by hand. `--format mermaid` renders a real flowchart;
  `depends_on` is one inferred (same-`path`) edge, documented as a
  heuristic, not real data/control-flow analysis.

```bash
# agent calls: fs.write_file(..., _belay_intent="auth-fix",
#              _belay_test_ref="tests/test_auth.py::test_login_bug")
belay causal s_abc123 --format mermaid
```
- **`belay rewind --intent/--keep`** undoes exactly one declared subgoal,
  keeping another, instead of a whole session or an arbitrary step cutoff.
  An agent tags a call by adding a reserved `_belay_intent` key to its
  arguments (stripped before the upstream ever sees it, recorded on that
  step's `plan_created` ledger event); `belay rewind --intent
  cache-refactor --keep auth-fix` then resolves to the exact `--to-step`
  cutoff that undoes only the `cache-refactor`-tagged steps. This only
  works when those steps are a safe contiguous trailing run — anything
  interleaved (an untagged step, a third subgoal after the one being kept)
  is refused outright (`rewind_intent_not_suffix`) rather than guessed at
  with a fabricated semantic merge; no LLM is involved in the decision,
  only in whatever agent chose the tag in the first place.

```bash
# agent calls: fs.write_file(path=auth.py, ..., _belay_intent="auth-fix")
#              fs.write_file(path=cache.py, ..., _belay_intent="cache-refactor")
belay rewind s_abc123 --intent cache-refactor --keep auth-fix --by jairo
# -> --intent 'cache-refactor' resolved to --to-step 1
# -> only cache.py's write is compensated; auth.py is untouched
```
- **`belay learn <approval_id>`** compiles a human's rejection into a
  durable, enforced rule — not agent memory, a real addition to an
  `IntentContract` (`forbidden_tools`/`forbidden_scope`) that every future
  session loading it (`belay run --intent-contract`) actually can't
  violate. Only two proposals are generated, both mechanical (no LLM
  interpreting the free-text rejection reason): forbid the rejected tool,
  or forbid its file scope. Printed by default; nothing is written until
  `--apply <kind> --intent-contract <file>` says so explicitly.

```bash
belay approvals reject ap_91f9f9b9f2 --by jairo \
  --reason "bulk_delete with unknown before_year is too risky"
belay learn ap_91f9f9b9f2 --db belay.db
# -> candidate rule(s): [forbidden_tools] crm.bulk_delete
belay learn ap_91f9f9b9f2 --db belay.db \
  --apply forbidden_tools --intent-contract learned.yaml
# -> every session with --intent-contract learned.yaml now denies
#    crm.bulk_delete outright, before the upstream ever sees it
```
- **`belay explore <session_id>...`** compares already-run session
  variants side by side — steps, distinct files touched, tools used, steps
  proven by a test vs not, unknown effects, and (with `--config`)
  irreversible/indeterminate step count via a real rewind dry-run per
  variant. Belay has no agent of its own to generate variants, so this
  doesn't run anything new — it assembles a comparison table from what
  each variant's session already produced (reusing `belay causal`'s graph
  and `belay rewind`'s dry-run). No LLM ranks the results; it's a table,
  not a verdict — a human picks.
- **`belay export-pr`** packages a committed session's file changes
  (the `path`/`content` shape `examples/contracts/fs.yaml` already uses) as
  a real git branch + commit, with Belay's own signed evidence (E13)
  attached under `.belay-evidence/`, and opens a PR via `gh` if it's on
  `PATH` (otherwise prints the exact `git push`/`gh pr create` to run).
  Post-hoc, not a pre-execution gate: the session already ran the full
  governed pipeline (contract → plan → policy → approval → saga commit);
  this turns that into a paper trail reviewable the way a human's change
  would be, backed by evidence rather than a trust-me summary. With
  `--intent-contract` and `--config`, the PR body becomes **proof-carrying**
  — it answers a reviewer's real questions instead of just listing files:
  What was asked? (the contract's `intent`) What changed without being
  asked? (files outside `allowed_scope`) What new behavior exists? (causal
  graph, per file) What was verified / what couldn't be proven?
  (`_belay_test_ref` present or not, per step) What external effects
  occurred? (each step's declared effects) How is this undone? (a real
  `belay rewind --dry-run` plan). Any section without enough data says so
  explicitly (`_not declared_`, `_not computed_`) instead of guessing.

```bash
belay export-pr s_ed182c2544f8 --repo ./infra-repo --db belay.db \
  --base main --key signing.pem
# -> 1 file change(s) found:  step 1: write configs/app.conf (fs.write_file)
# -> branch belay/s_ed182c2544f8 created (2 file(s) committed)
# -> gh CLI not found -- prints git push + gh pr create to run yourself
```
- **`belay replay`** re-executes a real session against the live upstream
  with one step's args overridden, producing a brand-new, fully real,
  ledgered session — not a simulation. Unlike `belay counterfactual` (E12,
  purely offline, never touches a real upstream), this is genuine
  "do it again, but differently starting here" debugging: every step from
  the original session's `plan_created` events replays in order, through
  the real `Lifecycle` (resolve → plan → policy → approval → execute); if a
  step pauses for approval, replay stops honestly and reports it —
  `--resume <replay_session_id>` continues after you resolve the pause via
  `belay approvals`.

```bash
belay replay s_5a814ae53684 --at-step 3 --override '{"before_year": 2021}' \
  --by jairo --config belay.wrap.json
# -> new session: replay_9b37580f2501 (replay of s_5a814ae53684)
#      step 1: crm.import_records
#        -> pending_approval (approval=ap_...) -- replay stops here
belay approvals approve ap_... --by jairo --db belay.db
belay replay s_5a814ae53684 --at-step 3 --override '{"before_year": 2021}' \
  --by jairo --config belay.wrap.json --resume replay_9b37580f2501
# -> resumes at step 2, applies the override at step 3, pauses/commits for real
```

## How it fits

```
Agent (LLM) ──MCP──▶ Belay ──MCP──▶ tool servers
                       │
   contracts · policy · planner · approvals · saga executor · rewind
                       │
                event ledger (append-only, hash-chained)
```

See [`docs/architecture.md`](docs/architecture.md) for the full diagram and
[`docs/spec.md`](docs/spec.md) §3 for the normative request lifecycle.

## Install

`belay-mcp` isn't published to PyPI/npm yet (see "Release status" below), so
`pip install belay-mcp` / `npx belay-mcp` don't work for anyone but the
maintainer. The one-line installers below install the exact same package
straight from GitHub instead — same `belay` command, no registry needed —
the same pattern `rustup`/`deno`/`uv` use before (or alongside) a package
manager release:

```bash
# macOS / Linux / WSL
curl -fsSL https://raw.githubusercontent.com/Jairogelpi/belay-mcp/main/scripts/install.sh | sh
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/Jairogelpi/belay-mcp/main/scripts/install.ps1 | iex
```

Both scripts: find a Python 3.12+ interpreter, install with `pipx` if it's
present (an isolated venv, `belay` on `PATH` — the standard way to install a
Python CLI tool without touching any project's own environment) or fall back
to `pip install --user`, then print the next command to run
(`belay bootstrap ...`, see below). Piping a downloaded script into a shell
requires trusting the source — read
[`scripts/install.sh`](scripts/install.sh)/[`scripts/install.ps1`](scripts/install.ps1)
first if you'd rather not; they're short and do nothing besides `pip`/`pipx`
install from this repo.

Once published, `pip install belay-mcp` / `npx belay-mcp ...` will work
directly — an npm wrapper (`npm/`) already exists for the latter; see
"Release status" below. It `pip install`s the matching version under the
hood and needs a Python 3.12+ interpreter on `PATH`.

For development:

```bash
git clone https://github.com/Jairogelpi/belay-mcp.git
cd belay-mcp
pip install -e ".[dev]"
pytest
```

### One-command setup: `belay bootstrap`

```bash
belay bootstrap ./sandbox --command npx --arg -y \
  --arg @modelcontextprotocol/server-filesystem --arg ./sandbox \
  --client all
```

Runs `draft-contracts` (skip with `--contracts <file>` if you already have
one), `wrap`, `init` against every known client (`--client
claude-desktop,claude-code,cursor,codex,opencode`, or `all`), and upserts
a standing-instruction block into `./AGENTS.md` and `./CLAUDE.md` telling
whatever agent reads them to use Belay's MCP tools **by default, without
being told each session** — those files are the one thing every agent
actually re-reads automatically, so that's where a durable instruction
has to live, not a one-off chat message. Safe to re-run: the block is
idempotent (marked, replaced in place, never duplicated), and other
content in those files is left untouched.

Registering Belay as an MCP server does not, by itself, mean an agent's
*every* tool call goes through it — Claude Code/Cursor/Codex/OpenCode can
still reach for their own native Bash/file-edit tools without touching MCP
at all. The `AGENTS.md`/`CLAUDE.md` instruction is the durable nudge; a
deterministic hook-based gate that actually intercepts native tool calls
too is `belay hooks install` (E18), below.

### Native Agent Gate: `belay hooks install` (E18)

```bash
belay hooks install --yes
# -> installed PreToolUse/PostToolUse hooks in .claude/settings.json
# -> approvals queued by this hook -- review with
#    belay hooks approvals list/approve/reject --db belay-hooks.db
#    (NOT `belay approvals ...` -- that group opens --db as a literal file;
#    hook-queued approvals live in this install's private belay home, see
#    "Local supervisor" below, so `belay hooks approvals` resolves --db the
#    same project-identity-anchor way `belay hooks run/rewind` already do)
# -> restart the agent for the hooks to take effect
```

First slice, said plainly rather than oversold: **Claude Code only** —
Bash (gated `allow`/`pause`), native `Edit`/`Write`/`NotebookEdit` (captured
for rewind, see below), and `PostToolUse` evidence recording; MCP tool
calls made through Claude Code's own client are not yet normalized by this
gate. Codex is not wired up yet (its hook surface couldn't be verified
against real behavior in time, only against docs that partially conflicted
with each other — better to ship a narrower, verified slice than a
broader, unverified one). Every Bash command Claude Code is
about to run goes through `belay/hooks/decision.py`, a deterministic (no
LLM) classifier: an explicit, narrow allowlist of read-only commands (`ls`,
`cat`, `git status`/`diff`/`log`/`show`, `grep`, `pytest`, `pwd`, `echo`) is
allowed; **everything else pauses**, including any allowlisted-looking
command combined with shell chaining/redirection/substitution (`;`, `&&`,
`|`, `` ` ``, `$()`, a newline) — those are rejected before the allowlist is
even checked, so `git status; rm -rf /` never slips through on the strength
of its safe-looking prefix. A paused command is queued as a real pending
item in the exact same `ApprovalQueue` (spec §7) `belay run`'s MCP path
uses — `belay approvals list/approve/reject --db belay-hooks.db` reviews it,
never a native "ask" prompt the agent's own client would show (that would
let a human bypass Belay's queue entirely). Once approved, the identical
command is allowed on retry; a rejected command stays denied without being
re-queued.

Same safety guarantees as `belay init`/`belay uninstall` (E17.1): atomic
write with backup, a `.belay-manifest.json`, preview + one confirmation, a
pre-write re-check that aborts on an external edit instead of clobbering it,
and `belay hooks uninstall`/`belay hooks doctor` with the identical
restore-vs-surgical/BROKEN-detection logic.

#### Local supervisor

Every decision is made by a persistent, authenticated **local supervisor**
(`belay/supervisor/`), not a fresh cold-start per tool call: `belay hooks
run` (what the hook config actually invokes) is a thin IPC client that
connects to it, spawning one on demand if none is running yet.

```bash
belay supervisor status --db belay-hooks.db   # running -- listening on ...
belay supervisor stop --db belay-hooks.db     # ask it to shut down
```

- **Never an unauthenticated TCP port** — the client talks to the
  supervisor over a Windows named pipe or a POSIX Unix domain socket (via
  Python's stdlib `multiprocessing.connection`, which does an HMAC
  challenge-response handshake with an installation-scoped capability token
  before any payload crosses the wire — no new dependency, no token ever
  sent in plaintext). The token lives under this user's private belay home
  (`~/.belay/keys/…` or `%LOCALAPPDATA%\belay\keys\…`, `0600` on POSIX,
  overridable via `BELAY_HOME`) — never inside the project, and a
  wrong-length/corrupted token file is discarded and regenerated rather
  than used as-is.
- **JSON on the wire, not pickle** (`belay/supervisor/wire.py`) — Python's
  `Connection.send()`/`.recv()` pickle their argument, and unpickling data
  from a peer that's merely *authenticated* (not necessarily trustworthy
  software) is a real code-execution risk; every message here is canonical
  UTF-8 JSON with a hard size cap, fully schema-validated before any object
  is built from it.
- **The approvals/idempotency database is private too** — always under this
  user's belay home, never inside the project a hook gates. An earlier
  version defaulted it into the project directory, which meant an agent
  with ordinary file-write access (or a Bash command the gate itself
  allowed) could reach the SQLite file directly and edit its own approval
  state by hand; `belay hooks install`'s own printed message shows the real
  path.
- **The classifier can't be pointed at its own secrets** — even an
  otherwise-allowlisted "safe read" command (`cat`, etc.) is denied if any
  of its path-looking arguments resolve into belay's private storage, since
  "outside the project" doesn't mean "outside what the OS user running the
  agent's Bash tool can read".
- **Fails closed.** If the supervisor can't be reached or a request times
  out, the answer is `deny`, not `allow` and not a hang — never leaves a
  `PreToolUse` call unanswered. A connected-but-silent peer (a local
  Slowloris) is actively timed out, not merely deprioritized, and can't
  block other clients — the supervisor accepts and handles connections
  concurrently (bounded worker pool of daemon threads, so a stuck
  connection can never keep the whole process from exiting on shutdown).
- **Duplicate event IDs are idempotent, durably** — stored in SQLite, not
  memory, so the exact same tool-call retried gets the exact same answer
  even across a supervisor restart (crash, upgrade, hard kill), not just
  within one process's lifetime; a key reused with genuinely different
  content is treated as a collision and denied, never answered from either
  version.
- **Approvals are bound to the full context they were granted in** — host,
  session, tool, the command itself, working directory, the repository's
  real git HEAD, and the decision-logic ruleset version — not the command
  text alone. Approving a command in one repository/branch/session never
  silently approves the identical string somewhere else.
- One supervisor per install (keyed by a resolved project-anchor path,
  hashed — never opened as a file itself), not a single global daemon
  shared across unrelated projects.
- Host-agnostic by construction: the supervisor normalizes every event into
  one common shape (spec §7.1-style: installation id, host/adapter version,
  correlation id, phase, surface, normalized tool identity, structured
  args, cwd/repo identity, OS user obtained independently of the payload,
  monotonic + wall-clock timestamps) before the classifier ever sees it —
  adding a host is a new adapter module, not a rewrite of the decision
  logic in `belay/hooks/gate.py`. `trust_tier` reports `T1` for Claude
  Code's Bash surface (E18.7, below) — every other host adapter still
  honestly reports `UNKNOWN` until each gets its own pinned-version
  conformance suite; a claim this project has been careful not to make
  early elsewhere shouldn't be made early here either.
- **`PostToolUse` records real evidence, into the same evidence system as
  the MCP path** (E18.2) — once the Bash tool actually runs, the supervisor
  appends the result (exit code, a computed duration correlated against the
  matching `PreToolUse` call, an output digest, truncation flag) to a
  durable, hash-chained `LedgerStore` — the exact same store/format `belay
  run`'s MCP path already uses, so `belay verify <the hook db>` works with
  zero changes. There's no allow/deny decision to make at this point (the
  action already happened), so the response is an empty ack. The precise
  sub-field names Claude Code uses inside its result payload
  (`tool_response`/`tool_result`, `exit_code`/`exitCode`, …) couldn't be
  pinned down with full confidence from available docs — extraction tries
  several plausible names defensively and never fabricates a value for a
  field it didn't actually find, said plainly rather than assumed correct.
- **Native `Edit`/`Write`/`NotebookEdit` calls are captured for rewind**
  (E18.3, spec FILE-001/002/004/005/006/008) — unlike Bash, these are
  **allowed by default** (gating every routine file edit would make the
  gate unusable for real coding); the supervisor instead snapshots the
  file's pre-edit content as a side effect of the `allow`, then records its
  post-edit hash once `PostToolUse` fires. `belay hooks list-edits --db
  belay-hooks.db` shows captured events; `belay hooks rewind <event-id>
  --db belay-hooks.db` restores the file to its pre-edit content (or
  deletes it, if the edit created a brand-new file) — but refuses with a
  plain `conflict` message rather than clobbering anything if the file's
  current content doesn't match what was recorded right after the edit
  (something else touched it since). Snapshots are content-addressed and
  deduplicated on disk under the same private belay home as the approvals
  database; a file over the 5&nbsp;MiB capture cap isn't silently allowed
  uncaptured — it pauses for approval instead, same as Bash's fail-safe
  posture. This is a deliberately separate, simpler mechanism from
  `belay/rewind/service.py` (the MCP-contract-based rewind used by `belay
  run`) — that service's capture/compensation/verification model is built
  around calling a tool to compensate; a native file edit has no tool to
  call back into, only direct file I/O, so a smaller purpose-built snapshot
  store fits better than forcing the contract shape onto it.
- **Native `mcp__<server>__<tool>` calls always pause, no exceptions**
  (E18.4) — when Claude Code's own client talks to an MCP server directly
  (its native MCP support, not `belay run`), that call never passes through
  belay's contract-enforcing proxy at all, no matter what the server is
  named. Rather than guess whether a given server is actually belay's own
  wrapped one (fragile, and a wrong guess would mean silently allowing an
  unaudited call), every native MCP call gets Bash's own default treatment:
  queued in the identical `ApprovalQueue`, bound to the full context (host,
  session, the specific `server__tool` identity, a canonical dump of its
  arguments, cwd, repo HEAD) — a different argument set for the same tool
  is a different approval, not an automatic pass. Review/approve/reject
  with `belay hooks approvals list/approve/reject --db belay-hooks.db`
  (distinct from `belay approvals`, see above).
- **`belay doctor` now flags MCP bypass routes** — any MCP server
  configured in a client's config alongside (or instead of) belay is
  reachable directly by that client, regardless of whether the Native
  Agent Gate hook is even installed for that host; `doctor` lists every
  other server name found so that exposure isn't invisible, whether or not
  belay itself is registered there.
- **Codex adapter (E18.5): normalize/render only, not wired to a live
  session, said plainly rather than oversold.** `belay/hooks/codex_adapter.py`
  normalizes Codex's own `ExecCommandApprovalParams`/`ApplyPatchApprovalParams`
  approval-request shapes into the exact same host-agnostic `HookEvent`
  Claude Code events become — the existing Bash classifier and file-edit
  gate run against a Codex-shaped event completely unmodified, confirming
  the host-agnostic design pays off. Every field name was read straight
  from `codex app-server generate-json-schema --experimental` run against
  the real installed `codex-cli` binary (not guessed from docs). What's
  *not* here yet: Codex's approval mechanism is a bidirectional JSON-RPC
  protocol inside a long-lived session (`codex app-server`), not a
  one-shot subprocess hook like Claude Code's — actually intercepting it
  live means belay driving or transparently proxying that whole session,
  materially more infrastructure than `belay hooks run`, closer in shape
  to `belay wrap`'s MCP proxy. Building that without running it end-to-end
  against a real session would repeat the exact mistake this project has
  avoided elsewhere (shipping an unverified guess as a working
  integration) — so this stops at tested normalize/render logic, not a
  live gate.
- **OpenCode adapter (E18.6): normalize/render only, and for a different
  reason than Codex.** `belay/hooks/opencode_adapter.py` normalizes real
  `tool.execute.before`/`tool.execute.after` calls into the same
  `HookEvent`; the Bash classifier runs against an OpenCode-shaped event
  unmodified, same as it does for Codex. Verified two ways against the
  actual installed `opencode-ai` binary (OpenCode ships no schema command
  like Codex's, so docs alone wouldn't have been enough): a real
  already-installed third-party plugin
  (`~/.config/opencode/plugins/engram.ts`) uses `tool.execute.after`'s
  exact shape in production, and the compiled binary itself was searched
  for the literal `W.trigger("tool.execute.before"/"after", ...)` call
  sites, confirming the `(input, output)` argument shape directly from the
  shipped code. The gap here isn't a missing proxy layer (Codex's problem)
  — it's that OpenCode's hooks are plain **in-process function calls** made
  by the binary into a TypeScript/JavaScript plugin module; there is no
  Python-reachable seam at all. Gating it for real means shipping an
  actual TS plugin package that calls out to belay's supervisor — a new
  language and packaging surface for this repo, not built here. Whether a
  plugin can actually *deny* a call by throwing inside
  `tool.execute.before` looks architecturally plausible from the bundle's
  control flow but was not proven against a live session.
- **Cursor: skipped, not guessed.** The installed `cursor` binary on this
  machine is only the GUI editor's launcher CLI (open a file, diff, add an
  MCP server to its config) — it exposes no way to headlessly verify
  Cursor Agent's actual tool-call hook payload shape the way Codex's own
  schema generator or OpenCode's installed plugin + compiled binary did.
  Rather than write an adapter against docs alone with no way to check it
  against real behavior (exactly what this project avoided doing for
  Claude Code's PostToolUse field names until the ambiguity could at least
  be flagged honestly), Cursor has no adapter yet.

#### Live conformance (E18.7): `trust_tier="T1"` for Claude Code's Bash surface, earned not assumed

`tests/hooks/test_live_conformance.py` is the spec §7.2 "pinned-version
end-to-end bypass suite" TRUTH-004 requires before a host can claim `T1` —
opt-in only (`pytest tests/hooks/test_live_conformance.py -m
live_conformance --no-cov`), never part of the default suite or CI, since
it spawns the real, installed `claude` CLI and spends real Anthropic API
usage. Pinned to the exact `claude --version` (`PINNED_CLAUDE_VERSION`)
it was verified against — a mismatch **skips**, never silently claims
conformance against a binary the suite never actually ran against.

What it actually proves, against a real session with belay's hooks
installed and Claude's own permission layer bypassed (so any block is
attributable to belay's hook, not Claude's separate permission prompt):
an unrecognized Bash command's side effect genuinely never happens on
disk — not "the model said it didn't run it" — while a real pending item
lands in the actual approval queue, and a safe command still reaches the
real host and returns real output (bypass resistance that doesn't just
mean "blocks everything"). `belay/hooks/claude_code_adapter.py`'s
`_VERIFIED_TRUST_TIER` is `"T1"` now specifically because this suite
exists and passed — scoped to Claude Code's Bash surface, since that's
what it exercises, not a blanket claim about Edit/Write/MCP surfaces or
any other host (those still honestly report `UNKNOWN`).

### Wrapping a non-Python MCP server

`belay wrap` defaults to launching `python <server_dir>/server.py`, but any
stdio MCP server works via `--command`/`--arg`:

```bash
belay wrap ./sandbox --contracts contracts/fs.yaml \
  --command npx --arg -y --arg @modelcontextprotocol/server-filesystem --arg ./sandbox
```

### Registering with an MCP client (Claude Desktop / Claude Code / Cursor / Codex / OpenCode)

```bash
belay init --client claude-desktop --config belay.wrap.json
belay init --client codex,opencode --config belay.wrap.json
```

Merges a `belay` entry into the client's own config, in its own native
format — other MCP servers already configured are left untouched, and
re-running is idempotent (replaces in place, never duplicates):

| Client | File | Format |
| --- | --- | --- |
| Claude Desktop | OS-specific (autodetected) | JSON `mcpServers` |
| Claude Code | `.mcp.json` (project root) | JSON `mcpServers` |
| Cursor | `.cursor/mcp.json` (project root) | JSON `mcpServers` |
| Codex CLI | `.codex/config.toml` (project root, default) or `~/.codex/config.toml` (`--scope user`) | TOML `[mcp_servers.<name>]` |
| OpenCode | `opencode.json` (project root) | JSON `mcp.<name>` (`command` as one array, not split `command`/`args`) |

Codex's TOML is edited with `tomlkit` (format-preserving — comments,
other tables, and formatting choices survive byte-for-byte; an earlier
regex-based merge was reproducibly broken by valid TOML like a comment on
the `[mcp_servers.belay]` heading line or an indented table right after
it). Every write in this section goes through an atomic temp-file +
rename with a `.belay-backup` of anything overwritten — a crash mid-write
never leaves a half-written config.

Restart the client afterward. `belay init` previews every file it would
touch and asks one confirmation before writing anything (`--dry-run` to
only preview, `--yes`/`-y` to skip the prompt for CI/scripts); a
`.belay-manifest.json` alongside each config records the before/after
content hash so `belay uninstall`/`belay doctor` never have to guess
whether the file changed since.

```bash
belay init --client claude-code --config belay.wrap.json --dry-run
# -> this will register 'belay' in 1 config file(s):
#      update: .mcp.json  (claude-code)
# -> --dry-run: nothing written

belay doctor --client claude-code
# -> claude-code: registered at .mcp.json -- unchanged since install, backup available

belay uninstall --client claude-code --yes
# -> unchanged since install -> restores the full pre-install backup
# -> modified since install (you added another MCP server, etc.) ->
#    surgically removes only the belay entry, leaving your other edits intact
```

### Drafting contracts from a live server

Writing a contract by hand per tool is real friction. `belay draft-contracts`
connects to the real upstream, reads each tool's MCP `readOnlyHint`/
`destructiveHint` and name (no LLM), and proposes a starting contract per
tool — read/write/delete tools sharing a resource name (`write_x`/`read_x`,
`delete_x`/`read_x`) are paired into capture+undo contracts, mirroring the
hand-written pattern in `examples/contracts/fs.yaml`; everything else
defaults to `irreversible`, the safe default when no undo path can be
inferred:

```bash
belay draft-contracts ./sandbox --command npx --arg -y \
  --arg @modelcontextprotocol/server-filesystem --arg ./sandbox \
  -o contracts_draft.yaml
```

Every draft is `provenance.verified: false` — review and correct each one
before use; nothing downstream trusts an unverified contract's correctness,
only its presence.

### Dashboard

```bash
belay dashboard --db belay.db -o dashboard.html
```

A static HTML snapshot of one ledger: every session's steps with their
verdicts, and pending approvals with the exact `belay approvals
approve/reject` command to run. No server, no live DB access from the page
(refresh by re-running the command); approval stays CLI-only and
human-typed by design (spec §7, §12 no-self-approval).

## Quickstart

```bash
belay wrap examples/fs-server --contracts examples/contracts/fs.yaml
belay run &
# any standard MCP client now talks to Belay instead of fs-server directly:
# tools with a contract or readOnlyHint pass through, everything else is
# refused with contract_missing (spec §4.6) — logged to belay.db either way.
belay verify belay.db
```

Every command above was re-run against a clean checkout while writing this
README; `belay verify belay.db` prints `chain: OK` / `coherence: OK` on an
empty, freshly-wrapped ledger.

## Demo

The 3-minute portfolio demo (spec-driven scenario in `docs/plan.md` §10) is
a real, runnable script — not a mock:

```bash
python examples/demo.py         # bulk delete -> pause -> narrow -> approve -> execute -> rewind
python examples/demo.py --oops  # same, plus a wrong-filter mistake that rewind then undoes
```

It shells out to the real `belay` CLI (`wrap`, `approvals list/approve`,
`rewind --dry-run`, `rewind --by`) and drives a real MCP session against
`examples/crm-mock`, ending in `chain: OK` / `coherence: OK` and "session
fully compensated" — genuine output, generated live each run.

`belay approvals approve --narrow <filter>` does not exist as CLI surface
(documented gap, see [ADR 0007](docs/adr/0007-e7-rewind.md) and
[ADR 0009](docs/adr/0009-e9-demo-docs-polish.md)); the demo's "narrowing"
step is the equivalent E7 actually built and tested — the agent retries with
a different, narrower filter, which is a new plan the human approves
instead of the original one.

**Recording:** a VHS tape script (`examples/demo.tape`) is checked in for
whoever has the `vhs` binary to render a GIF from — `asciinema` and `vhs`
were not available in the sandbox this entrega was built in, so no
recording is embedded here yet. This is an honest gap, not a placeholder
GIF; see ADR 0009.

## Roadmap

Built entrega by entrega per [`docs/plan.md`](docs/plan.md); each closes a
slice of [`docs/spec.md`](docs/spec.md):

| Entrega | Delivers | Spec sections | Status |
|---|---|---|---|
| E0 | Repo scaffolding, CI, tooling | — | done |
| E1 | Contracts + expression language | §4 | done |
| E2 | Event ledger | §9 | done |
| E3 | L1 proxy + CLI (first publishable milestone) | §3, §4.6, App. C | done |
| E4 | Planner + policy engine | §5, §6 | done |
| E5 | Approvals | §7 | done |
| E6 | Saga executor | §8 | done |
| E7 | Rewind (closes L3 conformance) | §10 | done |
| E8 | Public conformance suite + example packs | §13 | done |
| E9 | Demo, docs, portfolio polish, v0.1.0 release | — | done (tag/PyPI pending, see below) |
| E10 | Statistical anomaly baselines | — (plan-v2) | done |
| E11 | Real SQL dry-run adapter | §5.3 (extended) | done |
| E12 | Counterfactual replay | §9.4 (extended) | done |
| E13 | Signed, offline-verifiable evidence | §9 (extended) | done |
| E14 | Identity attribution | §9, §12 (extended) | done |
| E15 | Per-identity irreversible-action quota | §6 (extended) | done |
| E16 | Blast-radius self-explanation | §6, §7 (extended) | done |
| E17 | Safe installer lifecycle — manifest, `belay init --dry-run/--yes`, `belay uninstall`, `belay doctor`, reinstall-idempotent and crash-safe (E17.1 hardening) — plus `docs/traceability.md` generator, CI-enforced | §8 (plan.md), adoption/DX | done |
| E18 | Native Agent Gate: authenticated local supervisor (`multiprocessing.connection`, named pipe/Unix socket, fail-closed, bounded concurrency), `belay hooks install`, deterministic Bash risk classifier, context-bound approvals routed through the same `ApprovalQueue` as the MCP path. E18.1 hardening closed 8 P0s found in independent review: JSON wire format (not pickle), private off-project approvals storage, durable idempotency, full-context approval binding, belay-internal-path protection, honest `trust_tier`, Slowloris resistance, hard-kill recovery. E18.2: `PostToolUse` recording (exit code, duration, output digest) into a durable, hash-chained ledger — the *same* `LedgerStore`/`belay verify` as the MCP path, no new evidence format. E18.3: native `Edit`/`Write`/`NotebookEdit` capture-on-allow + content-addressed snapshot store + `belay hooks rewind`/`list-edits`, conflict-safe restore-or-delete compensation, oversized files pause instead of silently going uncaptured. E18.4: native `mcp__server__tool` calls pause and queue through the same `ApprovalQueue` (no free pass for a server merely named "belay" — this layer can't confirm a call actually reached belay's own proxy), reviewed via the new `belay hooks approvals` (hook-queued approvals live in the private belay home, not a literal `--db` file the top-level `belay approvals` opens); `belay doctor` now flags other MCP servers configured alongside belay as an ungated bypass route, belay-managed or not. E18.5: Codex adapter, normalize/render only (verified against the real installed `codex-cli` binary's own app-server JSON schema) — deliberately not wired to a live session, since Codex's approval mechanism is a bidirectional JSON-RPC protocol, not a one-shot hook subprocess; a real integration needs session proxy infrastructure this slice doesn't build, said plainly rather than claimed. E18.6: OpenCode adapter, normalize/render only, verified two ways against the real installed `opencode-ai` binary (an actually-installed third-party plugin's production usage, plus locating the literal `tool.execute.before`/`.after` trigger call sites inside the compiled bundle) — not wired live because OpenCode's hooks are in-process TS/JS plugin calls with no Python-reachable seam, a new language/packaging surface this slice doesn't build. Cursor: skipped outright — the installed `cursor` binary is only the GUI launcher CLI, with no way found to headlessly verify its actual agent hook payload shape, so no adapter was written against docs alone. E18.7: `tests/hooks/test_live_conformance.py`, spec §7.2's pinned-version end-to-end bypass suite, opt-in only (spends real Anthropic API usage) — spawns the real installed `claude` CLI with belay's hooks actually installed, confirms a denied Bash command's side effect genuinely never happens on disk (not just "the model said so") while a safe one still works normally; `claude_code_adapter._VERIFIED_TRUST_TIER` is now `"T1"` for Claude Code's Bash surface specifically, because this suite exists and passed, not asserted ahead of the evidence | §7 (extended), §9.2 (FILE-001–008), §12.1, ARCH-001–008 (adoption/DX) | **first slice** — Claude Code Bash surface is T1-verified; Edit/Write/MCP surfaces and Codex/OpenCode adapters remain UNKNOWN; Cursor not attempted |

## Conformance

Belay is **L3** conformant (contracts + plans/policy/approvals +
sagas/rewind, spec §13), verified by the `belay-conformance` suite:

```bash
belay-conformance run --target belay --level 3
```

`belay-conformance` is a separate, target-agnostic package: any MCP proxy
that implements the ~6-method `ConformanceTarget` adapter can run the same
suite against itself.

Beyond conformance, [`docs/traceability.md`](docs/traceability.md) proves the
narrower claim that *every normative MUST in `docs/spec.md` has at least one
real, named test* — a hand-curated list of MUSTs cross-referenced against
`@spec("X.Y")` markers on test functions, generated by
`scripts/traceability.py`. CI runs the generator and fails the build if any
MUST is left uncovered, so the claim can't silently rot into a stale doc.

## How Belay compares

Belay isn't a gateway, an observability tool, or an enterprise workflow
engine — it overlaps with pieces of each without being a drop-in
replacement for any:

- **MCP gateways/routers** (e.g. [mcp-gateway](https://github.com/lasso-security/mcp-gateway),
  various vendor "MCP proxy" products) focus on auth, rate limiting, and
  routing across multiple MCP servers. Belay assumes you already have (or
  don't need) that layer and adds contract-based reversibility on top —
  its concern is "what happens if this specific call was a mistake",
  not multiplexing.
- **Observability/tracing for agents** (e.g. [LangSmith](https://www.langchain.com/langsmith),
  [Langfuse](https://langfuse.com/)) record what an agent did after the
  fact. Belay's ledger (spec §9) is adjacent but exists to make actions
  *governable and undoable*, not to analyze prompts/latency/cost.
- **Enterprise workflow/saga engines** (e.g. [Temporal](https://temporal.io/),
  [AWS Step Functions](https://aws.amazon.com/step-functions/)) implement
  the saga pattern generally, for developer-authored workflows. Belay
  narrowly targets one thing: an *agent-invoked* MCP tool call, undone via
  a contract the tool integrator declares once — it is not a general
  orchestration engine and doesn't try to be.

## Release status

`v0.1.0` is tagged (`git tag v0.1.0`, on top of the full E0-E9 Definition of
Done, §0) but **not published to PyPI yet** — that's a manual step for the
maintainer (PyPI trusted publishing must be configured on the PyPI project
first; an agent cannot do that). `main` is currently ahead of the `v0.1.0`
tag with E10-E18 (see "What's new since v0.1.0" above); no new tag has been
cut for those yet. See
[`.github/workflows/release.yaml`](.github/workflows/release.yaml) and
[`CHANGELOG.md`](CHANGELOG.md).

An npm wrapper package (`npm/`, also `belay-mcp`) is written and locally
verified but likewise **not published to npm yet** — same manual step,
same reason.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md) (rules
for any human or AI agent working on this repo).

## License

MIT — see [`LICENSE`](LICENSE). The specification text
([`docs/spec.md`](docs/spec.md)) is additionally available under CC-BY-4.0.
