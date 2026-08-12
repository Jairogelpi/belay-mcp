# E22 Zero-Configuration Codex and Claude Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user with Codex and/or Claude already installed connect the current directory to a Belay-protected Filesystem MCP server with one `belay connect` command and safely undo it with `belay disconnect`.

**Architecture:** Keep `belay.cli.main` thin and put immutable models, client adapters, and transaction orchestration in focused modules. Build the complete desired state and complete a real MCP preflight before writes, then use compare-and-swap snapshots for each official-CLI or renderer mutation; the project-owned manifest is the only authority for repair/disconnect.

**Tech Stack:** Python 3.12/3.13, Typer, MCP Python SDK, official `codex`/`claude` CLIs, `tomlkit`, JSON, `importlib.resources`, pytest/AnyIO.

---

## File structure

- Create `belay/bundled_packs.py`: resolve and validate bundled Filesystem pack metadata.
- Create `belay/packs/filesystem/{pack.yaml,contracts.yaml}`: wheel-visible copy of the E20 pack.
- Create `belay/cli/connection_models.py`: canonical project identity, snapshots, targets, manifest schema, and read-only inspection result.
- Create `belay/cli/client_registration.py`: Codex/Claude command adapters and Claude Desktop fallback.
- Create `belay/cli/connection.py`: preflight, transaction, rollback, verification, and disconnect orchestration.
- Create `tests/packs/test_bundled_filesystem_pack.py`, `tests/cli/test_connection_models.py`, `tests/cli/test_client_registration.py`, `tests/cli/test_connection.py`, `tests/cli/test_connect_integration.py`.
- Modify `belay/cli/main.py`: expose thin `connect`/`disconnect` commands and surface connection state through `doctor`/`repair`.
- Modify `belay/cli/client_configs.py`: byte/hash CAS helpers and project-scoped hook merge/removal reuse.
- Modify `pyproject.toml`: ship pack data.
- Modify `.github/workflows/ci.yaml`: wheel-only connection smoke.
- Modify `README.md`, `docs/architecture.md`, `CHANGELOG.md`: document exact scope and limitations.

### Task 1: Bundle the pinned Filesystem pack

**Files:**
- Create: `belay/bundled_packs.py`
- Create: `belay/packs/__init__.py`
- Create: `belay/packs/filesystem/pack.yaml`
- Create: `belay/packs/filesystem/contracts.yaml`
- Create: `tests/packs/test_bundled_filesystem_pack.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing package-data tests**

Assert `filesystem_pack()` resolves through `importlib.resources`, metadata pins `2026.7.10`, contracts load, and packaged bytes match canonical `packs/filesystem/` files.

- [ ] **Step 2: Run red**

Run: `py -3.13 -m pytest tests/packs/test_bundled_filesystem_pack.py -q --no-cov`

Expected: FAIL because the bundled resource API/data do not exist.

- [ ] **Step 3: Add resources and resolver**

Return a typed value containing contracts path and exact upstream argv:

```python
("npx", "-y", "@modelcontextprotocol/server-filesystem@2026.7.10", str(project))
```

Reject drift between metadata and command construction.

- [ ] **Step 4: Build and inspect a wheel**

Run in PowerShell: `py -3.13 -m build --wheel; $wheel = Get-ChildItem dist -Filter '*.whl' | Select-Object -First 1; py -3.13 -m zipfile -l $wheel.FullName`

Expected: both Filesystem YAML files appear under `belay/packs/filesystem/`.

- [ ] **Step 5: Commit**

```bash
git add belay/bundled_packs.py belay/packs tests/packs/test_bundled_filesystem_pack.py pyproject.toml
git commit -m "feat: bundle the pinned filesystem pack"
```

### Task 2: Define deterministic identity and manifest models

**Files:**
- Create: `belay/cli/connection_models.py`
- Create: `tests/cli/test_connection_models.py`

- [ ] **Step 1: Write failing path/name tests**

Cover POSIX case preservation, Windows `normcase`, separator normalization, ASCII slug collapse/truncation, first eight lowercase SHA-256 hex characters, `--name` validation, and distinct paths with the same basename.

- [ ] **Step 2: Write failing manifest tests**

Cover `connecting`, `connected`, `rollback_incomplete`, and `disconnected`; exact snapshot bytes encoded losslessly; target post-write hashes; UTC timestamps; unknown schema version rejection; and a typed `ConnectionInspection` that reports per-target healthy/missing/modified/conflict state without writing.

- [ ] **Step 3: Run red**

Run: `py -3.13 -m pytest tests/cli/test_connection_models.py -q --no-cov`

Expected: import failure.

- [ ] **Step 4: Implement frozen dataclasses/Pydantic-free JSON models**

Keep serialization explicit and deterministic. Use atomic writes for `.belay/connection.json` and never include secrets.

- [ ] **Step 5: Run green and commit**

Run: `py -3.13 -m pytest tests/cli/test_connection_models.py -q --no-cov`

```bash
git add belay/cli/connection_models.py tests/cli/test_connection_models.py
git commit -m "feat: model managed client connections"
```

### Task 3: Build official client registration adapters

**Files:**
- Create: `belay/cli/client_registration.py`
- Create: `tests/cli/test_client_registration.py`
- Modify: `belay/cli/client_configs.py`

- [ ] **Step 1: Write fake executable fixtures**

Fake `codex`, `claude`, and `npx` executables log argv and mutate only isolated config files. Never invoke the user's real home.

- [ ] **Step 2: Write failing command tests**

Require:

```text
codex mcp add <name> -- <launch-command> <launch-args...>
claude mcp add --scope user --transport stdio <name> -- <launch-command> <launch-args...>
```

Also test `mcp get/list/remove`, nonzero exit, timeout, missing executable, and collision output.

- [ ] **Step 3: Write failing Claude Desktop fallback tests**

The fallback atomically merges only `mcpServers.<name>` and preserves unrelated JSON. It is used only for detected Claude Desktop, which has no registration CLI.

- [ ] **Step 4: Run red**

Run: `py -3.13 -m pytest tests/cli/test_client_registration.py -q --no-cov`

- [ ] **Step 5: Implement adapters and byte snapshots**

Every adapter declares the files it may touch before mutation, captures bytes/existence/SHA, records post-write bytes/SHA, and exposes verify/remove. Use argument arrays with `shell=False`.

- [ ] **Step 6: Run green and commit**

```bash
git add belay/cli/client_registration.py belay/cli/client_configs.py tests/cli/test_client_registration.py
git commit -m "feat: add Codex and Claude registration adapters"
```

### Task 4: Generate and preflight the exact protected proxy command

**Files:**
- Create: `tests/cli/test_connect_integration.py`
- Create: `belay/cli/connection.py`
- Modify: `belay/proxy/upstream.py` only if a reusable timeout-safe preflight helper is required.

- [ ] **Step 1: Write failing runtime tests**

Assert `.belay/belay.wrap.json` uses the bundled contracts, `.belay/belay.db`, exact pinned upstream argv, and canonical current directory. For `sys.frozen`, assert the registered launch command is the absolute binary; otherwise assert the absolute interpreter plus `-m belay.cli.main`.

- [ ] **Step 2: Write a failing real MCP-through-Belay preflight**

Write the proposed runtime into the isolated project, start the exact launch argv that will be registered (`<python> -m belay.cli.main run --config ...` or the frozen executable form), initialize that stdio process with the MCP SDK, and call `list_tools` through Belay. Assert the advertised tools came from the pinned Filesystem upstream. Close Belay, its upstream, and every stream. Skip only when `npx` is absent; CI must install Node and must not skip.

- [ ] **Step 3: Run red**

Run: `py -3.13 -m pytest tests/cli/test_connect_integration.py -q --no-cov`

- [ ] **Step 4: Implement runtime generation and preflight**

Perform all dependency, contract, directory, and upstream checks before client mutation. After runtime files exist, independently launch the exact proposed registered Belay command and complete initialize/list-tools through the proxy before committing the connection. Bound both stages with timeouts and include the exact failed dependency/stage in the error.

- [ ] **Step 5: Prove directory confinement**

Through the generated Belay proxy (never by connecting the test directly to the upstream), list/read inside `tmp_path` successfully and prove an outside path is unavailable or rejected.

- [ ] **Step 6: Commit**

```bash
git add belay/cli/connection.py belay/proxy/upstream.py tests/cli/test_connect_integration.py
git commit -m "feat: preflight the protected filesystem runtime"
```

### Task 5: Implement transactional `connect`

**Files:**
- Modify: `belay/cli/connection.py`
- Create/Modify: `tests/cli/test_connection.py`

- [ ] **Step 1: Write the success and idempotency tests**

Detect only installed targets; require at least one; configure every detected target; install project hooks at `<project>/.claude/settings.json` only when Claude Code is detected; verify all registrations; mark manifest `connected`; second call is no-op or safe repair.

- [ ] **Step 2: Write failure-injection tests**

Parametrize failure after each runtime write, client registration, hook write, and verification. Assert reverse rollback restores exact bytes/absence and reports the original failure.

- [ ] **Step 3: Write concurrent-edit tests**

Mutate a target after Belay's post-write snapshot but before rollback. Assert Belay does not overwrite it, exits nonzero, names the file/registration, and persists `rollback_incomplete`.

- [ ] **Step 4: Write failing inspection and proxy-verification tests**

Require the public typed operation `inspect_connection(project_dir, name=None)`. It must be read-only, report every manifest/runtime/client/hook drift, identify `rollback_incomplete`, and distinguish a safe repair from a concurrent-edit conflict. The successful connect test must spawn the exact command read back from every fake client's recorded registration and complete MCP initialize/list-tools through Belay before accepting `connected`.

- [ ] **Step 5: Run red**

Run: `py -3.13 -m pytest tests/cli/test_connection.py -q --no-cov`

- [ ] **Step 6: Implement inspection and the state machine**

Implement `connect(project_dir, name=None)`, `disconnect(project_dir, name=None)`, and `inspect_connection(project_dir, name=None)`. Use this fixed order: resolve project → validate dependencies → build desired state → upstream preflight → detect collisions → snapshot every target → write atomic runtime/connecting manifest → register all detected clients → install Claude project hooks → launch the exact registered Belay argv and verify MCP initialize/list-tools plus each official client registration → mark connected. Missing clients are skipped before the transaction; any detected target failure fails the whole transaction.

- [ ] **Step 7: Run green and commit**

```bash
git add belay/cli/connection.py tests/cli/test_connection.py
git commit -m "feat: connect detected clients transactionally"
```

### Task 6: Implement ownership-safe `disconnect`

**Files:**
- Modify: `belay/cli/connection.py`
- Modify: `tests/cli/test_connection.py`

- [ ] **Step 1: Write failing disconnect tests**

Cover healthy removal, unrelated entries preserved, changed managed entry conflict, missing manifest, repeat disconnect, retained database, retained runtime by default, and `--purge-runtime` removing only wrap+manifest after successful client/hook removal. Start from both `connected` and `rollback_incomplete`: safe targets are reconciled, conflicting concurrent edits are never overwritten, unresolved targets keep `rollback_incomplete`, and a later retry after the conflict is resolved reaches `disconnected`.

- [ ] **Step 2: Run red**

Run: `py -3.13 -m pytest tests/cli/test_connection.py -k disconnect -q --no-cov`

- [ ] **Step 3: Implement compare-and-swap removal**

Remove only when project/name/recorded post-write hash still match. Default writes `status: "disconnected"` plus UTC `disconnected_at`. Never remove `.belay/belay.db`.

- [ ] **Step 4: Run green and commit**

```bash
git add belay/cli/connection.py tests/cli/test_connection.py
git commit -m "feat: disconnect only Belay-managed entries"
```

### Task 7: Expose thin CLI commands

**Files:**
- Modify: `belay/cli/main.py`
- Modify: `tests/cli/test_main.py`
- Create: `tests/cli/test_connect_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Check `belay connect --help`, no-arg defaults, `--name`, `--project`, `--client codex|claude|all`, `disconnect --purge-runtime`, concise success summary, collision error, no-client error, and rollback-conflict exit code. With a current-project connection manifest, `belay doctor` must render `inspect_connection` without writing; `belay repair` must safely call `connect` for repairable drift and refuse a `rollback_incomplete` concurrent-edit conflict with instructions to reconcile via `disconnect`.

- [ ] **Step 2: Run red**

Run: `py -3.13 -m pytest tests/cli/test_connect_cli.py tests/cli/test_main.py -q --no-cov`

- [ ] **Step 3: Add command wrappers**

The Typer functions parse options, call the orchestration service, render results, and translate typed connection errors to exit 1. Do not duplicate transaction logic in `main.py`.

- [ ] **Step 4: Run green and commit**

```bash
git add belay/cli/main.py tests/cli/test_connect_cli.py tests/cli/test_main.py
git commit -m "feat: expose connect and disconnect commands"
```

### Task 8: Add wheel, client-syntax, and documentation evidence

**Files:**
- Modify: `.github/workflows/ci.yaml`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `CHANGELOG.md`
- Create: `scripts/smoke_connect.py`
- Create: `tests/tools/test_smoke_connect.py`

- [ ] **Step 1: Write the isolated smoke driver and tests**

The driver accepts Belay executable, fake client bin directory, isolated home/config roots, and project path; it runs connect twice, reads the exact launch argv recorded by the fake client, spawns that argv as an MCP stdio server, completes initialize/list-tools through Belay, proves current-directory confinement through Belay, disconnects, and verifies unrelated config survives. Connecting directly to the Filesystem upstream does not satisfy this smoke.

- [ ] **Step 2: Run driver tests red then green**

Run: `py -3.13 -m pytest tests/tools/test_smoke_connect.py -q --no-cov`

- [ ] **Step 3: Extend `wheel-smoke`**

Install Node, build/install only the wheel, place fake Codex and Claude CLIs on PATH, set isolated homes, and execute `scripts/smoke_connect.py`. A missing bundled pack, wrong CLI syntax, wrong recorded Belay launch command, failure to list tools through Belay, or failed confinement must fail the job.

- [ ] **Step 4: Document the exact one-command experience**

Show `belay connect`/`disconnect`, existing-client prerequisite, Filesystem-only current-directory default, pinned upstream, project-derived names, Claude hook scope, Codex MCP-only protection, and retained evidence DB.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yaml README.md docs/architecture.md CHANGELOG.md scripts/smoke_connect.py tests/tools/test_smoke_connect.py
git commit -m "ci: verify zero-config wheel connections"
```

### Task 9: Verify and prepare the E22 PR

- [ ] **Step 1: Run focused and static gates**

Run: `ruff check . && mypy belay && py -3.13 -m pytest tests/cli/test_connection_models.py tests/cli/test_client_registration.py tests/cli/test_connection.py tests/cli/test_connect_cli.py tests/cli/test_connect_integration.py tests/packs/test_bundled_filesystem_pack.py --no-cov`

Expected: PASS with branch coverage gate satisfied by the full project run if the focused run is below the global threshold; use `--no-cov` for focused iteration.

- [ ] **Step 2: Run global gates**

Run: `py -3.13 -m pytest && py -3.13 -W error::ResourceWarning -m pytest -m "" --no-cov && py -3.13 scripts/traceability.py --check && py -3.13 -m conformance.cli run --target belay --level 3`

Expected: all pass.

- [ ] **Step 3: Run isolated official syntax smoke**

Invoke installed `codex` and `claude` with isolated config/home environment variables and a temporary project; never read or mutate personal configuration. Verify `mcp add/get/remove` syntax and `belay connect` end to end.

- [ ] **Step 4: Push and open PR E22**

Branch from the merged E21 `main`. The PR body links E22, lists red/green tests, includes wheel/MCP smoke evidence, and explicitly states that Codex has no claimed native hook integration.
