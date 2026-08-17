# Belay

[![CI](https://github.com/Jairogelpi/belay-mcp/actions/workflows/ci.yaml/badge.svg)](https://github.com/Jairogelpi/belay-mcp/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Conformance: L3](https://img.shields.io/badge/conformance-L3-brightgreen.svg)](conformance)

> PyPI badge intentionally omitted: `belay-mcp` is not published yet (see
> "Release status" below) — a badge pointing at a nonexistent PyPI project
> would 404, so it's left out rather than faked.

**Safe, reversible tool execution for AI agents.**

Belay is a transactional safety layer for AI agent tool calls, in two
complementary modes: an **MCP proxy** that sits between an agent and its
tool servers (mediated — Belay itself makes the call), and a **Native
Agent Gate** (`belay hooks install`) that gates an agent's own native
tool calls in place (observed — Claude Code makes the call, Belay decides
whether it's allowed to). Either way, the goal is the same: turn "the
agent can call anything" into "every tool call is declared, previewable,
gated, and — when it goes wrong — reversible."

> Status: **`v0.1.0` tagged, L3 conformance.** E0-E9 (`docs/plan.md`) shipped
> the full lifecycle — contracts (§4), ledger (§9), the L1 proxy (§3, §4.6,
> Appendix C), planner + policy (§5, §6), approvals (§7), the saga executor
> (§8), and rewind (§10), diagrammed in
> [`docs/architecture.md`](docs/architecture.md). Eleven further entregas
> (E10-E20, `docs/plan-v2.md`) shipped past v0.1.0 without breaking L3 — see
> "What's new since v0.1.0" below (E18 is a first slice — Claude Code only,
> said plainly in its own section below; R1.6, see [`CHANGELOG.md`](CHANGELOG.md),
> closed six correctness gaps in that gate). Passing on real
> Linux/macOS/Windows CI (not just one dev machine — see E19.7 below and the
> CI badge above for the current run), plus
> [`docs/traceability.md`](docs/traceability.md) proving every normative MUST
> in the spec has a real test (CI-enforced, not a stale doc). The protocol is
> specified in [`docs/spec.md`](docs/spec.md) (Belay Specification 0.1).
> **Branch coverage: 81.34% repo-wide**, measured 2026-08-12 (`fail_under =
> 81`, CI-enforced floor against regressions — raised as more lands, never
> lowered silently; see [ADR 0027](docs/adr/0027-e21-release-truth.md)). The
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

### Zero-config: `belay connect` (E22)

```bash
belay connect
# ... some time later, or on a different machine:
belay disconnect
```

The one-command version of the above for the single most common case —
**you already have Codex CLI and/or Claude Code CLI (and/or Claude
Desktop) installed** and want the current directory's files protected by
Belay with no flags at all. `belay connect`, run with no arguments:

- Detects which of Codex CLI, Claude Code CLI, and Claude Desktop are
  actually installed on this machine — at least one is required; it never
  invents a client that isn't there.
- Generates a real, protected proxy for the current directory only — the
  **Filesystem MCP server** (`@modelcontextprotocol/server-filesystem`),
  **pinned** to the exact version this pack was verified against
  (`packs/filesystem/pack.yaml`) — not "whatever `npx` resolves today".
  No other upstream server is offered by `connect`; wrapping something
  else still means `belay wrap`/`belay init` (above) by hand.
- Names the connection deterministically from the project directory (an
  ASCII slug of its basename plus an 8-hex-char hash of its full resolved
  path — see `belay/cli/connection_models.py`), so re-running `connect` in
  the same directory is idempotent and two different directories that
  happen to share a basename never collide. Override with `--name`.
- Registers with each detected client through **that client's own
  official CLI** (`codex mcp add ... -- ...`, `claude mcp add --scope user
  --transport stdio ... -- ...`) — never by hand-editing
  `~/.codex/config.toml`/`~/.claude.json` itself. Claude Desktop has no
  registration CLI, so it's the one exception: a surgical JSON merge of
  just `mcpServers.<name>`, leaving everything else in that file
  untouched.
- For Claude Code specifically, also installs a **project-scoped**
  `PreToolUse`/`PostToolUse` hook at `<project>/.claude/settings.json` (not
  a global/user-scope hook) — the same Native Agent Gate `belay hooks
  install` provides, scoped to just this project.
- **Codex gets MCP-only protection** — there is no Codex-side hook
  mechanism this integrates with (said plainly: Codex has no claimed
  native hook integration), so only tool calls that actually go through
  the registered MCP server are gated; Codex's own native Bash/file-edit
  tools are not.
- Proves the exact command it's about to register actually works, twice:
  once before touching any client (a real MCP `initialize`/`list_tools`
  through the generated proxy), and once after, reading back each
  client's own recorded registration rather than assuming it matches.
- Every tool call goes through the same append-only, hash-chained ledger
  (`.belay/belay.db`) as `belay wrap`/`belay run` — `belay disconnect`
  never deletes it, with or without `--purge-runtime`.

`belay disconnect` removes only the entries `belay connect` itself
registered (compare-and-swap: an entry hand-edited since is left alone,
reported, never silently overwritten) and leaves `.belay/belay.wrap.json`
and `.belay/belay.db` in place unless you pass `--purge-runtime`, which
still never touches the ledger database. `belay doctor`/`belay repair`
understand this connection's health too — see their `--help`.

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

## Advanced setup

### Native Agent Gate: `belay hooks install` (E18)

```bash
belay hooks install --yes
# -> installed PreToolUse/PostToolUse hooks in .claude/settings.json
# -> approvals queued by this hook -- review with
#    belay hooks approvals list/approve/reject --db belay-hooks.db
# -> restart the agent for the hooks to take effect
```

A deterministic (no LLM), no-cold-start local supervisor gates an agent's
*native* tool calls (Bash, file edits, and native MCP calls made outside
`belay run`'s own proxy) -- **Claude Code only, first slice.** Bash
commands are classified against a narrow read-only allowlist (everything
else pauses); native `Edit`/`Write`/`NotebookEdit` calls are allowed by
default and captured for rewind (`belay hooks rewind <event_id>`); native
`mcp__server__tool` calls always pause unconditionally, since they never
pass through Belay's own contract-enforcing proxy. See the CHANGELOG's E18
entry for the full detail (supervisor authentication, the 8 P0s E18.1
hardening closed, and what's still `UNKNOWN` trust tier), and
[`docs/security/threat-model.md`](docs/security/threat-model.md) for what
this gate does and does not protect against.

```bash
belay supervisor status --db belay-hooks.db   # running -- listening on ...
```

**Opt-in contract check** (R1 first slices, [ADR 0021](docs/adr/0021-r1-native-gate-contract-check.md)):
`--contracts <file>` makes native file edits resolve against a real
`ContractSet`, the same way `belay run`'s MCP proxy resolves a tool -- a
tool with no matching contract denies (`contract_missing`) instead of the
default allow. The same file also lets a declared, all-read native MCP
tool (`mcp__server__tool`) auto-allow instead of always pausing --
anything not explicitly declared read-only still pauses exactly as
before. Off unless you opt in. Note this resolves by the tool's *literal
Claude Code name* (`Write`/`Edit`/`NotebookEdit`, or `mcp__<server>__<tool>`
for native MCP calls) -- not a downstream MCP server's own tool names, so
`packs/filesystem/contracts.yaml` (which declares `read_file`/`write_file`/
etc. for the *proxy* path) is not a valid argument here:

```bash
belay hooks install --contracts packs/claude-code-native/contracts.yaml --yes
```

That pack covers the three native file-edit tools. Auto-allowing a native
MCP tool call is install-specific -- it needs a contract keyed by *your*
server's actual name, e.g. a `mcp__github__list_issues` entry with an
all-`read` effects list, added to the same contracts file.

**Session fencing** (R1 third slice, [ADR 0022](docs/adr/0022-r1-native-gate-session-fencing.md)):
`belay hooks fence <host_session_id>` closes a hook session to every
surface -- Bash, file edits, native MCP -- the same durable, cross-process
way `belay rewind` already fences an MCP session. No `unfence`; start a
new session with the agent instead.

```bash
belay hooks fence s1 --host claude-code --db belay-hooks.db --yes
```

**Per-OS-user quota** (R1 fourth slice, [ADR 0023](docs/adr/0023-r1-native-gate-quota.md)):
once an OS user has this many *approved* hook-gated actions within the
window, a new pause-worthy action (Bash, native MCP, oversized file edit)
denies outright instead of being queued -- an operator must intervene
directly. Off unless you opt in:

```bash
belay hooks install --quota-max 20 --quota-window 1d --yes
```

**Extra Bash allowlist** (R1 fifth slice, [ADR 0024](docs/adr/0024-r1-native-gate-configurable-allowlist.md);
exact-match syntax added R1.6): add your own literal, safe commands to the
built-in read-only allowlist -- not a `PolicyEngine` for Bash, just an
extensible list of commands your project already trusts. Entries are
checked after the same shell-metacharacter guard everything else is, so
they can never become a chaining/redirection bypass. A bare entry also
allows any trailing arguments (`npm run lint` allows `npm run lint --fix`
too) -- append `!` to require an exact match instead, since a trailing
argument can turn a read-only command into a mutating one:

```bash
echo "npm run lint!" > my-safe-commands.txt   # exact match only -- `--fix` still pauses
belay hooks install --allowlist-extra my-safe-commands.txt --yes
```

#### Live conformance (E18.7): `trust_tier="T1"` for Claude Code's Bash surface

`tests/hooks/test_live_conformance.py` is a real, opt-in (spends real
Anthropic API usage), pinned-version end-to-end suite against the actual
installed `claude` CLI, confirming a denied Bash command's side effect
genuinely never happens on disk. `"T1"` is earned by this suite existing
and passing, not asserted ahead of the evidence -- every other surface
(Edit/Write/MCP, and the Codex/OpenCode adapters) still honestly reports
`UNKNOWN`. See [`docs/adapter-compatibility.md`](docs/adapter-compatibility.md)
for the full per-host, per-surface matrix.

### Wrapping a non-Python MCP server

```bash
belay wrap ./sandbox --contracts contracts/fs.yaml \
  --command npx --arg -y --arg @modelcontextprotocol/server-filesystem --arg ./sandbox
```

### Verified action packs (E20)

`packs/filesystem/` and `packs/git/` are real, tested contract sets for the
actual official `@modelcontextprotocol/server-filesystem` (npm) and
`mcp-server-git` (PyPI) servers -- not the illustrative `contracts/fs.yaml`
above:

```bash
belay wrap ~/projects/my-repo --contracts packs/filesystem/contracts.yaml \
  --command npx --arg -y --arg @modelcontextprotocol/server-filesystem --arg ~/projects/my-repo
```

See [ADR 0019](docs/adr/0019-e20-verified-packs-scope.md) for scope and
known limitations per pack.

### Registering with an MCP client

```bash
belay init --client claude-desktop --config belay.wrap.json
belay init --client codex,opencode --config belay.wrap.json
```

Merges a `belay` entry into the client's own config, in its own native
format -- other MCP servers already configured are left untouched, and
re-running is idempotent:

| Client | File | Format |
| --- | --- | --- |
| Claude Desktop | OS-specific (autodetected) | JSON `mcpServers` |
| Claude Code | `.mcp.json` (project root) | JSON `mcpServers` |
| Cursor | `.cursor/mcp.json` (project root) | JSON `mcpServers` |
| Codex CLI | `.codex/config.toml` (project root, default) or `~/.codex/config.toml` (`--scope user`) | TOML `[mcp_servers.<name>]` |
| OpenCode | `opencode.json` (project root) | JSON `mcp.<name>` |

`belay init` previews every file it would touch and asks one confirmation
before writing (`--dry-run` to only preview, `--yes` to skip the prompt);
`belay doctor`/`belay uninstall`/`belay repair` use a `.belay-manifest.json`
alongside each config to know exactly what changed since install. `belay
detect`/`--client auto` registers only clients actually installed on this
machine. See the CHANGELOG's E19 entry for `disable-bypass`, `hooks doctor
--deep`, `repair`, native binaries, and signed release bundles.

### Drafting contracts from a live server

```bash
belay draft-contracts ./sandbox --command npx --arg -y \
  --arg @modelcontextprotocol/server-filesystem --arg ./sandbox \
  -o contracts_draft.yaml
```

Reads each tool's MCP `readOnlyHint`/`destructiveHint` and name (no LLM)
and proposes a starting contract per tool. Every draft is
`provenance.verified: false` -- review and correct each one before use.

### Dashboard

```bash
belay dashboard --db belay.db -o dashboard.html
```

A static HTML snapshot of one ledger: every session's steps with their
verdicts, and pending approvals with the exact command to resolve them.

## What's new since v0.1.0

Seven of the eleven entregas that landed on top of the v0.1.0 lifecycle
(`docs/plan-v2.md`, ADRs 0010-0018) are spec-numbered lifecycle extensions,
listed below; the remaining four (E17-E20) are adoption/DX work, covered in
the "Adoption/DX" subsection right after. Each is additive — none weakened
an existing test or broke L3
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

Full detail and examples for each of these are in
[`CHANGELOG.md`](CHANGELOG.md)'s "Adoption/DX" section:

- **`belay wrap --command/--arg`** launches any stdio MCP server, not just
  `python server.py`.
- **`belay draft-contracts`** proposes a starting contract per upstream
  tool from its live MCP schema (see "Drafting contracts" above).
- **`belay dashboard`** renders a static HTML snapshot of a ledger (see
  "Dashboard" above).
- **`belay approvals list --triage`** sorts the pending queue highest-risk
  first — never approves or rejects anything itself.
- **Intent contracts** (`belay run --intent-contract <file>`) mechanically
  enforce `allowed_scope`/`forbidden_scope`/`forbidden_tools`/
  `budgets.files_changed`, hash-pinned from session start so `belay
  export-pr` can label a PR's "what was asked" as verified or not.
- **`belay verify-test --runner pytest|jest|go`** independently runs a
  step's declared test instead of trusting the agent's own claim it
  passed.
- **`belay causal <session>`** assembles a requirement → decision → test →
  undo graph straight from the ledger (`--format mermaid`).
- **`belay rewind --intent/--keep`** undoes exactly one agent-tagged
  subgoal while keeping another.
- **`belay learn <approval_id>`** compiles a human's rejection into a
  durable, mechanical `IntentContract` rule.
- **`belay explore <session_id>...`** compares already-run session
  variants side by side — a table, not an LLM verdict.
- **`belay export-pr`** packages a committed session's file changes as a
  real git branch + commit with signed evidence attached, and (with
  `--intent-contract`/`--config`) a proof-carrying PR body.
- **`belay replay`** re-executes a real session against the live upstream
  with one step's args overridden, through the real governed lifecycle.

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
| E18 | Native Agent Gate: authenticated local supervisor (`multiprocessing.connection`, named pipe/Unix socket, fail-closed, bounded concurrency), `belay hooks install`, deterministic Bash risk classifier, context-bound approvals routed through the same `ApprovalQueue` as the MCP path. E18.1 hardening closed 8 P0s found in independent review: JSON wire format (not pickle), private off-project approvals storage, durable idempotency, full-context approval binding, belay-internal-path protection, honest `trust_tier`, Slowloris resistance, hard-kill recovery. E18.2: `PostToolUse` recording (exit code, duration, output digest) into a durable, hash-chained ledger — the *same* `LedgerStore`/`belay verify` as the MCP path, no new evidence format. E18.3: native `Edit`/`Write`/`NotebookEdit` capture-on-allow + content-addressed snapshot store + `belay hooks rewind`/`list-edits`, conflict-safe restore-or-delete compensation, oversized files pause instead of silently going uncaptured. E18.4: native `mcp__server__tool` calls pause and queue through the same `ApprovalQueue` (no free pass for a server merely named "belay" — this layer can't confirm a call actually reached belay's own proxy), reviewed via the new `belay hooks approvals` (hook-queued approvals live in the private belay home, not a literal `--db` file the top-level `belay approvals` opens); `belay doctor` now flags other MCP servers configured alongside belay as an ungated bypass route, belay-managed or not. E18.5: Codex adapter, normalize/render only (verified against the real installed `codex-cli` binary's own app-server JSON schema) — deliberately not wired to a live session, since Codex's approval mechanism is a bidirectional JSON-RPC protocol, not a one-shot hook subprocess; a real integration needs session proxy infrastructure this slice doesn't build, said plainly rather than claimed. E18.6: OpenCode adapter, normalize/render only, verified two ways against the real installed `opencode-ai` binary (an actually-installed third-party plugin's production usage, plus locating the literal `tool.execute.before`/`.after` trigger call sites inside the compiled bundle) — not wired live because OpenCode's hooks are in-process TS/JS plugin calls with no Python-reachable seam, a new language/packaging surface this slice doesn't build. Cursor: skipped outright — the installed `cursor` binary is only the GUI launcher CLI, with no way found to headlessly verify its actual agent hook payload shape, so no adapter was written against docs alone. E18.7: `tests/hooks/test_live_conformance.py`, TRUTH-004's pinned-version end-to-end bypass suite, opt-in only (spends real Anthropic API usage) — spawns the real installed `claude` CLI with belay's hooks actually installed, confirms a denied Bash command's side effect genuinely never happens on disk (not just "the model said so") while a safe one still works normally; `claude_code_adapter._VERIFIED_TRUST_TIER` is now `"T1"` for Claude Code's Bash surface specifically, because this suite exists and passed, not asserted ahead of the evidence | §7 (extended); FILE-001–008, ARCH-001–008, TRUTH-004/010 ([ADR 0020](docs/adr/0020-extended-requirement-catalog.md), not `docs/spec.md`) (adoption/DX) | **first slice** — Claude Code Bash surface is T1-verified; Edit/Write/MCP surfaces and Codex/OpenCode adapters remain UNKNOWN; Cursor not attempted |
| E19 | One-command lifecycle and exclusive routing. E19.1: `belay detect`/`belay init --client auto` — real binary-presence + version detection (`shutil.which`, best-effort `--version`) instead of blindly registering every client type regardless of whether it's installed; caught and fixed a real P0 during manual verification (a loop variable shadowing the `--name` parameter, which briefly mis-registered a real machine's Claude Desktop config under the wrong key before being caught and reverted from its own backup). E19.2: `belay disable-bypass` — the write half of E18.4's bypass detection, atomically removing one named non-belay MCP server entry from a client config, without attempting to auto-rewrap the removed server. E19.3: `belay hooks doctor --deep` — real reachability checks (interpreter exists and can import belay, supervisor genuinely reachable) instead of only comparing config-file hashes; opt-in since it's not purely read-only. E19.4: `belay repair` — detects every belay-managed registration gone BROKEN (MCP clients and hooks) and restores all of them in one command, reusing `init`'s/`hooks install`'s own atomic-write machinery. E19.5: standalone `belay`(`.exe`) binaries via PyInstaller (`scripts/build_binary.py`), built and smoke-tested on real Linux/macOS/Windows CI runners — ~500 MB, said plainly (heavy transitive deps via `--collect-all`, size-reduction is real follow-up work, not done here). E19.6: `belay release sign`/`verify` — Ed25519 authenticity signing of a release bundle (SHA256SUMS.txt + signature + public key), reusing E13's `SigningKey`/`verify_signature`; explicitly NOT OS-level code-signing/notarization (Authenticode/Apple notarization need a paid, identity-verified certificate this project doesn't have — Windows SmartScreen/macOS Gatekeeper will still warn regardless of this signature). E19.7: `cross-platform-clean-room` CI job — the fast suite on real ubuntu-latest/macos-latest/windows-latest runners, verified by CI actually passing there, not asserted from one dev machine | ARCH-007/008 ([ADR 0020](docs/adr/0020-extended-requirement-catalog.md), not `docs/spec.md` §14, which has no subsections), adoption/DX | done |
| E20 | Verified action packs — real, useful transactions, not just a framework. Scoped to Filesystem + Git (user's choice — GitHub/Odoo need real API credentials/an instance this environment doesn't have, and the spec's own exit criterion requires a real disposable service, not mocks). `packs/filesystem/` and `packs/git/` — real `Contract` sets (loaded via the existing `belay/contracts/loader.py`, no new loader) targeting the actual official `@modelcontextprotocol/server-filesystem` (npm) and `mcp-server-git` (PyPI) servers, hand-corrected past `belay draft-contracts`'s naive heuristic (`edit_file`/`move_file` made properly reversible; `create_directory` fixed from a nonsensical draft undo to honestly `irreversible`, since the real server has no delete tool at all; `write_file` is `conditional` — reversible only if the file already existed). Git pack is smaller by real necessity, not by choice: `mcp-server-git` returns plain text only, and the contract expression grammar deliberately has no string-parsing capability, so most of its mutations (`git_commit`/`git_create_branch`/`git_checkout`) are honestly `irreversible` — only `git_add` (undone via the argument-free `git_reset`) is reversible. `tests/packs/test_filesystem_pack.py`/`test_git_pack.py`: E20's own exit criterion applied literally — a real multi-step saga against the real server, a real injected mid-saga failure, `auto_compensate` verified against real on-disk/on-repo state, plus a drift check that the pack's tool list matches what the real server actually advertises. `pack.yaml` per pack is metadata only — the full spec §11 packaging infrastructure (signed registry index, trust states, revocation, a pack-install CLI, an authoring SDK) is real, separate infrastructure work with its own hosting/trust-root decisions, not built here; both packs honestly declare `trust_state: unverified`. See [ADR 0019](docs/adr/0019-e20-verified-packs-scope.md) | §11, adoption/DX | **partial** — Filesystem + Git packs done and tested against real servers; GitHub/Odoo packs and the full packaging/registry infrastructure not attempted |

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

`v0.1.0` is tagged (`git tag v0.1.0`) and immutable, but it is a **historical,
incomplete** release candidate: it contains package version `0.1.0.dev0`, its
PyPI Trusted Publishing workflow failed because Trusted Publishing was never
configured (so it did not publish to PyPI), and it did not meet the
historical global Definition of Done (§0) — 90% branch coverage and a
clean-clone test run under 60 seconds. See
[ADR 0027](docs/adr/0027-e21-release-truth.md) for the full record; it will
not be moved, recreated, or force-updated to change that history.

`main` is currently ahead of `v0.1.0` with E10-E20 (see "What's new since
v0.1.0" above). `v0.2.0a1` is the planned next GitHub prerelease — see
[`.github/workflows/release.yaml`](.github/workflows/release.yaml) and
[`CHANGELOG.md`](CHANGELOG.md); it has not been cut yet, and no GitHub
Release or PyPI publication should be assumed to exist until it is.

An npm wrapper package (`npm/`, also `belay-mcp`) is written and locally
verified but likewise **not published to npm yet** — same manual step,
same reason.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md) (rules
for any human or AI agent working on this repo).

## Security

See [`SECURITY.md`](SECURITY.md) for how to report a vulnerability and
[`docs/security/threat-model.md`](docs/security/threat-model.md) for what
Belay's Native Agent Gate and MCP proxy actually protect against.

## License

MIT — see [`LICENSE`](LICENSE). The specification text
([`docs/spec.md`](docs/spec.md)) is additionally available under CC-BY-4.0.
