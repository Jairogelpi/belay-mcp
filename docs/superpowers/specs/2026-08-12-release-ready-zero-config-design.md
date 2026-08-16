# Release-Ready Zero-Configuration Connection Design

**Status:** Approved by the maintainer on 2026-08-12

## Goal

Make Belay honest and polished enough for a public prerelease while giving a
user who already has Codex and/or Claude installed one command that creates a
protected filesystem MCP connection without requiring manual TOML, JSON, hook,
or contract configuration.

The primary user experience is:

```text
belay connect
```

The command protects the current directory with the packaged Filesystem
contract pack, connects every detected supported Codex/Claude client, verifies
the resulting MCP connection, and reports exactly which native surfaces are and
are not gated.

## Non-goals

- Installing or authenticating Codex, Claude Code, Claude Desktop, Node.js, or
  npm.
- Claiming that Codex native shell or file-edit tools are intercepted. Codex is
  protected only when it calls the Belay MCP server until Codex exposes a
  verified native approval integration that Belay implements.
- Publishing to PyPI before the maintainer configures PyPI Trusted Publishing.
- Rewriting or force-moving the existing public `v0.1.0` tag.
- Silently weakening the historical `docs/plan.md` v0.1.0 Definition of Done.
- Building the full signed contract-pack registry described by the protocol.

## Existing foundations

Belay already has most of the required primitives:

- `belay wrap` creates a `WrapConfig` for an upstream MCP server.
- `belay init` atomically renders client configurations and records backups and
  manifests.
- `belay detect`, `belay doctor`, `belay repair`, and `belay uninstall` provide
  lifecycle operations.
- `belay hooks install` gates supported Claude Code native surfaces.
- `packs/filesystem/contracts.yaml` is exercised against the real official
  `@modelcontextprotocol/server-filesystem` server.
- Codex and Claude Code both expose official `mcp add`, `get`, `list`, and
  `remove` CLI operations.

The new design composes these foundations instead of introducing a second
proxy, policy engine, contract format, or evidence path.

## Architecture

### 1. Connection orchestrator

A focused module under `belay/cli/` owns connection orchestration rather than
adding more lifecycle logic directly to the already-large `belay/cli/main.py`.
It exposes typed operations used by thin Typer commands:

- `connect(project_dir, name=None)`
- `disconnect(project_dir, name=None)`
- `inspect_connection(project_dir, name=None)`

The module receives command execution and path-discovery dependencies so tests
can exercise the complete transaction with fake Codex/Claude executables and
isolated homes. Production uses real subprocesses and platform paths.

### 2. Default protected server

With no upstream arguments, Belay launches:

```text
npx -y @modelcontextprotocol/server-filesystem@2026.7.10 <absolute-current-directory>
```

The generated state lives under `<project>/.belay/`:

- `belay.wrap.json` — absolute upstream command, directory, contract path, and
  ledger path.
- `belay.db` — the evidence ledger.
- `connection.json` — Belay-owned lifecycle manifest.

The verified Filesystem contracts are packaged as Belay package data so an
installed wheel does not depend on the repository's top-level `packs/`
directory. The source pack remains the canonical authoring copy; a packaging
test prevents the bundled copy from drifting.

The exact upstream version is read from the packaged pack metadata and pinned
in the generated command. `connect` fails before writing if the metadata has no
exact verified version. It never substitutes npm's floating `latest` release,
because a newer tool schema could invalidate the contracts being installed.

The Filesystem pack's existing limitations remain visible. In particular,
creating a new file or directory cannot always be compensated because the
upstream server lacks delete operations, and captured file content is not a
general secret-redaction boundary. `connect` reports that scope instead of
claiming complete native-tool isolation.

### 3. Client registration strategy

Registration is hybrid and uses each client's supported public surface:

- **Codex CLI, IDE extension, and desktop app:** invoke `codex mcp add` in the
  user configuration (`~/.codex/config.toml`). Those clients share the same
  Codex MCP configuration.
- **Claude Code:** invoke `claude mcp add --scope user --transport stdio` so a
  cloned project does not require an additional project-MCP approval. The CLI
  owns its user registration in `~/.claude.json`.
- **Claude Desktop:** use Belay's existing atomic JSON merge, backup, and
  manifest machinery because it has a separate desktop configuration file.
- **Claude Code native gate:** install the already-supported hooks after MCP
  registration in `<project>/.claude/settings.json`, not in a user-global hook
  entry. The connection manifest records that exact path and the pre-write
  snapshot. Each connected project therefore owns its own hook configuration
  and database anchor. No Codex-native hook claim is added.

The default registration name is derived deterministically from the canonical
project path: `belay-<sanitized-directory-name>-<path-hash-8>`. Canonicalization
uses `Path.resolve(strict=False)`, converts separators to `/`, and applies
`os.path.normcase` on Windows while preserving POSIX case. The hash is the first
eight lowercase hexadecimal characters of SHA-256 over the UTF-8 canonical
path. The slug is lowercase ASCII letters, digits, and hyphens, collapsed and
truncated to 32 characters. This lets several projects coexist in user-level
Codex and Claude Code configurations. `--name` may override it, but the same
collision rules still apply.

Source/wheel installations launch the current interpreter with an absolute
wrap path:

```text
<python> -m belay.cli.main run --config <project>/.belay/belay.wrap.json
```

When `sys.frozen` is true, registrations instead launch the standalone binary
directly:

```text
<absolute-belay-or-belay.exe> run --config <project>/.belay/belay.wrap.json
```

No registration emitted by a frozen binary may reference a Python interpreter
or `belay.cli.main`. The release matrix runs an end-to-end `connect` smoke with
each frozen Linux, macOS, and Windows artifact, a real pinned Filesystem MCP
handshake, a fake detected client, and an isolated home/config directory.

An unmanaged server with the requested name, or a Belay-managed server whose
manifest names a different canonical project path, is a hard conflict. Belay
never removes or overwrites it. A previously Belay-managed registration for the
same project is left unchanged when healthy or repaired from an exact pre-write
snapshot when broken.

### 4. Transaction and rollback

`belay connect` follows a prepare/validate/commit sequence:

1. Detect Codex, Claude Code, Claude Desktop, Node.js, and `npx` without
   writing.
2. Resolve and validate the packaged contract set.
3. Build the proposed wrap and connection manifest in memory.
4. Start the proposed MCP command and complete a real MCP initialize/tools-list
   handshake.
5. Detect unmanaged or cross-project name collisions in every target client.
6. Snapshot the exact bytes, existence state, and SHA-256 of every client and
   hook config file that an official CLI or Belay renderer may change.
7. Write `.belay/` state atomically.
8. Register each detected client.
9. Install project-scoped Claude Code hooks when Claude Code is detected.
10. Verify each registration through the official client CLI where available
   and through the independent MCP handshake.
11. Commit the connection manifest only after every required verification
    passes.

Every client that passes preflight detection is a required target. Missing
clients are skipped before the transaction, but a registration or verification
failure for any detected target fails the whole operation. At least one target
must be detected.

Immediately before each write, the command compares the current file to its
preflight snapshot and aborts that write on mismatch. Immediately after each
successful client CLI or renderer action, it records the exact resulting bytes
and SHA-256. Rollback proceeds in reverse order and restores the pre-transaction
bytes (or absence) only when the current file still equals that action's
recorded post-write bytes. A mismatch is a rollback conflict: Belay does not
overwrite the concurrent edit, reports the affected path and managed server
name, exits nonzero, and leaves the manifest in `rollback_incomplete` state for
`doctor`/`disconnect` to reconcile. Official CLI `remove` operations are not
treated as sufficient rollback because they cannot reproduce comments,
ordering, or other concurrent client settings. Pre-existing healthy
registrations are never changed and are not included in rollback.

The same compare-and-swap rules apply to `.belay/` runtime files. The command
reports both the original failure and every rollback conflict or failure.

`belay disconnect` reads the connection manifest and removes only matching
Belay-managed registrations and project hooks. By default it retains all files
under `.belay/`, including the evidence ledger, and changes `connection.json`
to `status: "disconnected"` with a UTC `disconnected_at` timestamp so ownership
and future safe reconnection remain provable. `--purge-runtime` additionally
removes only `.belay/belay.wrap.json` and `.belay/connection.json` after client
and hook removal succeeds. It never deletes `.belay/belay.db`; this first slice
has no evidence-deletion option.

### 5. Verification and user output

Success output is concise and evidence-based:

```text
Filesystem proxy: connected (C:\work\project)
Codex: connected via user MCP configuration
Claude Code: connected; native hooks installed
Claude Desktop: connected
Ledger: C:\work\project\.belay\belay.db

Codex native shell/edit tools are outside this MCP gate.
```

Missing clients are reported as skipped. The command succeeds only when every
detected supported Codex/Claude target is connected and verified, and fails
before writing when none is installed.

## Public release truth

The existing `v0.1.0` tag remains immutable. Public documentation will record
that it points to `0.1.0.dev0`, was a release candidate, and did not publish to
PyPI. A GitHub release note may be attached to the existing tag to make that
history explicit, but it must not describe the artifact as a successful PyPI
release.

The next release is `v0.2.0a1`, matching Python's `0.2.0a1` and npm's
`0.2.0-alpha.1`. It is a GitHub prerelease. The release workflow always builds
and attaches source, wheel, and the three supported binary artifacts built on
`ubuntu-latest`, `macos-latest`, and `windows-latest`. PyPI publishing is gated
by the repository variable `PYPI_PUBLISH_ENABLED == "true"`; an absent or false
value skips the job. The release runbook requires that variable to be absent or
false for `v0.2.0a1` unless the maintainer has first configured and tested the
trusted publisher.

Before the tag is created, the default branch contains and tests both release
entry points: the normal tag-push trigger and a `workflow_dispatch` recovery
trigger. Recovery accepts an existing tag name, resolves it from the remote,
verifies that it is an immutable `v*` tag, checks out the tag's exact commit,
builds artifacts from that checkout, and attaches them to the matching GitHub
prerelease. The dispatcher workflow itself therefore comes from the repaired
default branch while every release input comes from the immutable tag. It never
moves, recreates, or force-updates the tag.

An ADR records that the historical v0.1.0 global exit criteria of 90% branch
coverage and a sub-60-second clean-clone test run were not achieved. The target
is not silently lowered or retroactively declared complete. The README reports
the measured gate and current alpha status, and CI begins measuring branch
coverage explicitly. Any enforceable floor is based on a fresh measured
baseline and may only move upward.

Hard-coded test totals and wall-clock promises are removed from public copy.
CI remains the source for current counts and timing.

### Plan change gate

Before production implementation begins, `docs/plan.md` is amended in its own
commit with three new entregas and explicit `(d)` exit clauses matching the
delivery boundaries below. The maintainer must approve that commit after seeing
the exact text. The ADR documents the historical discrepancy but does not
supersede the plan. No implementation commit may build on the revised release
semantics before that approval.

The initiative is split because connection behavior, resource/test cleanup, and
external release operations are independently testable and the repository
requires one entrega per pull request:

- **E21 — Quality and release truth:** Windows Bash regression, SQLite resource
  lifecycle, explicit branch-coverage baseline, public-document corrections,
  security-reporting text, and the release-truth ADR.
- **E22 — Zero-configuration Codex/Claude connection:** packaged pinned
  Filesystem pack, `connect`/`disconnect`, transactional registration, hooks,
  isolated client integration tests, and wheel smoke.
- **E23 — Portfolio prerelease and governance:** release workflow/artifacts,
  real demo GIF, GitHub private reporting, branch protection, historical
  `v0.1.0` note, and `v0.2.0a1` prerelease runbook/execution.

## Security and repository governance

- Enable GitHub private vulnerability reporting.
- Make `SECURITY.md` and `CONTRIBUTING.md` consistently forbid public exploit
  details and point to the private channel.
- Deliver E21, E22, and E23 through three sequential pull requests.
- After E23 integration, protect `main` by requiring a pull request and these
  successful check contexts: `test (3.12)`, `test (3.13)`, `wheel-smoke`, all
  three `cross-platform-clean-room (...)` matrix jobs, and all three
  `build-binaries (...)` matrix jobs. A second human approval is not required
  because this is currently a single-maintainer repository.
- Preserve administrator recovery/bypass so a malformed ruleset cannot lock
  the maintainer out.
- Future entregas follow the PR rule; history is not fabricated with synthetic
  pull requests.

## Quality cleanup

### Windows shell test

The installer syntax test sends the script body to `bash -n` over stdin. This
works with Git Bash and WSL Bash and avoids translating a Windows path into the
wrong Unix path dialect.

### SQLite resource lifecycle

The test suite's repeated unclosed-SQLite warnings are treated as a defect, not
hidden with a warning filter. The implementation will identify the owning
SQLAlchemy engines/connections, add an explicit close/dispose lifecycle at the
smallest shared boundary, and update fixtures/callers to use it. A regression
test must first reproduce the warning or prove disposal.

### Documentation consistency

The README, changelog, contribution guide, pull-request template, release
status, roadmap counts, coverage claims, and R1.7.4 wording are reconciled with
the actual repository. Exact totals that become stale are replaced by commands
or durable statements.

### Demo asset

The checked-in VHS scenario is rendered from the real demo into a compressed
GIF under `docs/assets/`. The README embeds it near the first runnable example.
The recording is regenerated from committed commands rather than hand-edited.

## Testing strategy

All behavioral code follows red-green-refactor TDD.

1. **Pure unit tests:** detection decisions, command construction, packaged
   pack resolution, manifest validation, collision behavior, state transitions,
   and rollback plans.
2. **CLI integration tests:** fake `codex`, `claude`, and `npx` executables log
   arguments and simulate success/failure without touching the real user home.
3. **MCP integration:** a real filesystem server handshake proves that the
   generated wrap exposes tools through Belay.
4. **Lifecycle regression:** repeated connect is a no-op/repair; disconnect
   removes only managed entries; partial failure rolls back.
5. **Packaging smoke:** build and install only the wheel in an isolated
   environment, then resolve the bundled pack and execute the connection
   preflight.
6. **Platform regression:** the shell syntax test runs with both Git Bash and
   WSL-compatible stdin behavior.
7. **Global gates:** Ruff, strict mypy, fast and full pytest suites, explicit
   branch coverage, traceability, L3 conformance, the real demo, and release
   artifact smoke tests.
8. **Live local smoke:** after isolated tests pass, exercise the installed
   Codex and Claude CLIs with isolated configuration homes. The real personal
   configurations are not used for testing.

## Rollout order

1. Commit the `docs/plan.md` E21-E23 amendment separately and obtain explicit
   maintainer approval.
2. Implement, review, and merge E21. Record its clean CI run URL.
3. Implement, review, and merge E22. Record its clean CI and isolated
   Codex/Claude smoke evidence.
4. Implement, review, and merge E23. Before changing external state, assert:
   the worktree is clean; local `main` equals `origin/main`; every required
   check on that SHA succeeded; Python/npm versions match `v0.2.0a1`; the tag
   does not exist locally or remotely; and `PYPI_PUBLISH_ENABLED` is absent or
   false.
5. Enable GitHub private vulnerability reporting and read the setting back.
6. Create an honest GitHub prerelease note on the existing `v0.1.0` tag that
   records `0.1.0.dev0` and the failed/unconfigured PyPI publication. Do not
   move the tag.
7. Push `v0.2.0a1` at the verified `main` SHA. The release workflow must attach
   the sdist, wheel, Linux binary, macOS binary, and Windows `.exe`; verify all
   five artifact classes are present before considering E23 complete.
8. Create the `main` protection rule with pull-request enforcement, zero
   required human approvals, administrator bypass, and the exact check contexts
   listed above. Read the rule back and preserve its JSON as operator evidence.
9. If release artifact verification fails, do not move or recreate the tag.
   Mark the GitHub prerelease as failed/incomplete, fix the workflow through a
   new PR, then invoke the repaired default-branch `workflow_dispatch` recovery
   path with the same immutable tag. Verify the resolved tag SHA and all five
   artifact classes. PyPI remains disabled until its trusted publisher is
   separately configured and tested.
