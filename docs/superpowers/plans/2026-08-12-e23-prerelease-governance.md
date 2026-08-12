# E23 Portfolio Prerelease and Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an honest, recoverable `v0.2.0a1` GitHub prerelease with source/wheel/three working binaries, a reproducible demo GIF, private security reporting, and protected `main`.

**Architecture:** Put all version/tag/ref validation in a testable Python preflight used by both tag and manual recovery paths. Let the default-branch workflow accept an immutable tag, resolve and verify its SHA, then build every artifact from that checkout; keep PyPI as an explicit repository-variable gate and perform repository settings only after all three delivery PRs merge.

**Tech Stack:** GitHub Actions/CLI/API, Python `packaging`, Hatch/build, PyInstaller, Node/npx, VHS, Markdown, PowerShell/bash runbook commands.

---

## File structure

- Create `scripts/release_preflight.py`: validate tag, Python/npm versions, SHA, PyPI gate, and artifact inventory.
- Create `tests/tools/test_release_preflight.py`: local git-repository and artifact tests.
- Rewrite `.github/workflows/release.yaml`: tag and recoverable workflow-dispatch release.
- Modify `.github/workflows/ci.yaml`: frozen-binary connect smoke on all OSes.
- Create `docs/release-runbook.md`: immutable-tag, recovery, settings, and evidence procedure.
- Create `docs/assets/belay-demo.gif`: rendered real demo.
- Modify `examples/demo.tape`, `README.md`, `CHANGELOG.md`, `SECURITY.md`: final public release state.

### Task 1: Implement release preflight and inventory validation

**Files:**
- Create: `scripts/release_preflight.py`
- Create: `tests/tools/test_release_preflight.py`

- [ ] **Step 1: Write failing version tests**

Cover valid `v0.2.0a1` ↔ Python `0.2.0a1` ↔ npm `0.2.0-alpha.1`; malformed tag; tag/version mismatch; dirty tree; local/remote-main mismatch; existing local/remote tag; and `PYPI_PUBLISH_ENABLED` absent/false/true.

- [ ] **Step 2: Write failing ref-resolution tests**

In a temporary git remote, assert only an existing annotated or lightweight `v*` tag is accepted, its exact remote SHA is printed for workflow output, and a branch/ambiguous ref is rejected.

- [ ] **Step 3: Write failing artifact inventory tests**

Require exactly these five classes: sdist, wheel, Linux binary, macOS binary, Windows `.exe`; reject missing, duplicate/ambiguous, zero-byte, and version-mismatched artifacts.

- [ ] **Step 4: Write failing required-check tests**

Inject a fake `gh api` command runner and require these exact successful, completed check-run names on the candidate SHA: `test (3.12)`, `test (3.13)`, `wheel-smoke`, `cross-platform-clean-room (ubuntu-latest)`, `cross-platform-clean-room (macos-latest)`, `cross-platform-clean-room (windows-latest)`, `build-binaries (ubuntu-latest)`, `build-binaries (macos-latest)`, and `build-binaries (windows-latest)`. Fail on missing, queued, in-progress, cancelled, skipped, neutral, stale-SHA, duplicate ambiguous, or unsuccessful results, and include each missing/non-success context in the error.

- [ ] **Step 5: Run red**

Run: `py -3.13 -m pytest tests/tools/test_release_preflight.py -q --no-cov`

- [ ] **Step 6: Implement subcommands**

Provide `prepare --tag --repo`, `required-checks --repo --sha`, `resolve-tag --tag`, and `verify-artifacts --tag --directory`. `prepare` must resolve the candidate `origin/main` SHA and invoke `required-checks` for that exact SHA before it can succeed. Use argument arrays for git/gh, fail closed, and emit machine-readable JSON plus human errors.

- [ ] **Step 7: Run green and commit**

```bash
git add scripts/release_preflight.py tests/tools/test_release_preflight.py
git commit -m "feat: validate immutable release inputs"
```

### Task 2: Build a recoverable release workflow

**Files:**
- Rewrite: `.github/workflows/release.yaml`
- Create: `tests/tools/test_release_workflow.py`

- [ ] **Step 1: Write failing workflow-contract tests**

Parse the YAML as text/structured data and assert: tag push plus required `workflow_dispatch.inputs.tag`; resolve job invokes `resolve-tag`; every build checkout uses the resolved SHA; source and three OS binaries are uploaded; release job downloads/verifies all artifacts; PyPI job condition is exactly `vars.PYPI_PUBLISH_ENABLED == 'true'`; GitHub release is marked prerelease for alpha tags.

- [ ] **Step 2: Run red**

Run: `py -3.13 -m pytest tests/tools/test_release_workflow.py -q --no-cov`

Expected: FAIL against the current tag-only/PyPI-unconditional workflow.

- [ ] **Step 3: Implement resolve and test jobs**

For push use `github.ref_name`; for dispatch use `inputs.tag`. Resolve the remote tag from the workflow's default-branch code, export tag/SHA, checkout the SHA, install dev dependencies, and run the same required quality gates.

- [ ] **Step 4: Implement source and binary matrix builds**

Build sdist/wheel once and PyInstaller binary on `ubuntu-latest`, `macos-latest`, `windows-latest`. Each binary runs `--help` and the E22 isolated `smoke_connect.py` with real pinned Filesystem MCP plus a fake detected client.

- [ ] **Step 5: Implement release assembly and optional PyPI**

Download all artifacts, run `verify-artifacts`, create/update the matching GitHub prerelease, upload without clobbering unrelated files, and only then optionally publish sdist/wheel to PyPI when the repository variable is true.

- [ ] **Step 6: Run green and commit**

```bash
git add .github/workflows/release.yaml tests/tools/test_release_workflow.py
git commit -m "ci: make prereleases recoverable by immutable tag"
```

### Task 3: Promote frozen `connect` smoke to the required CI matrix

**Files:**
- Modify: `.github/workflows/ci.yaml`
- Modify: `scripts/smoke_connect.py`
- Modify: `tests/tools/test_smoke_connect.py`

- [ ] **Step 1: Add a failing frozen-launch assertion**

The smoke must inspect the fake client's recorded registration and assert it launches the absolute `belay`/`belay.exe` directly with `run --config ...`, never `python`, `py`, or `belay.cli.main`.

- [ ] **Step 2: Run the smoke test red if frozen mode is not supported**

Run: `py -3.13 -m pytest tests/tools/test_smoke_connect.py -q --no-cov`

- [ ] **Step 3: Extend every `build-binaries` matrix job**

Install Node, execute the built artifact's `connect` against a temporary project and real pinned Filesystem server, verify list-tools and directory confinement, then disconnect. Use OS-native path handling.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yaml scripts/smoke_connect.py tests/tools/test_smoke_connect.py
git commit -m "ci: exercise frozen zero-config connections"
```

### Task 4: Render and embed the real demo

**Files:**
- Modify: `examples/demo.tape`
- Create: `docs/assets/belay-demo.gif`
- Modify: `README.md`

- [ ] **Step 1: Make the tape self-contained**

Set output to `docs/assets/belay-demo.gif`, run committed commands from repository root, use deterministic terminal dimensions/timing, and remove the stale comment saying VHS was unavailable.

- [ ] **Step 2: Run the underlying demo first**

Run: `py -3.13 examples/demo.py --oops`

Expected: real pause/approval/execution/rewind narrative completes and chain/coherence verification passes.

- [ ] **Step 3: Render with VHS**

Run: `vhs examples/demo.tape`

Expected: `docs/assets/belay-demo.gif` exists, is nonempty, and visibly shows the actual committed demo output.

- [ ] **Step 4: Inspect and compress without changing content**

Open the GIF, confirm readable text/no clipping, and use a lossless or visually equivalent GIF optimizer if it materially reduces size. Do not hand-edit frames.

- [ ] **Step 5: Embed near the first runnable README example**

Use a relative Markdown image link and state the exact regeneration command.

- [ ] **Step 6: Commit**

```bash
git add examples/demo.tape docs/assets/belay-demo.gif README.md
git commit -m "docs: embed the reproducible Belay demo"
```

### Task 5: Write the operator runbook and final release copy

**Files:**
- Create: `docs/release-runbook.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `SECURITY.md`

- [ ] **Step 1: Document the pre-tag gate**

Include exact commands for clean worktree, `main == origin/main`, required GitHub checks on the SHA, Python/npm version match, nonexistent tag, and disabled/absent PyPI variable.

- [ ] **Step 2: Document failure recovery**

Never move/recreate the tag. Mark release incomplete, repair workflow through a new PR, dispatch the default-branch workflow with the same tag, compare resolved SHA, and re-run five-class inventory verification.

- [ ] **Step 3: Document GitHub settings/readbacks**

Include private vulnerability reporting enable/read commands and main protection/ruleset create/read commands with the exact check contexts from E23. State administrator bypass and zero required human approvals.

- [ ] **Step 4: Prepare release notes**

Create honest notes for historical `v0.1.0` (`0.1.0.dev0`, failed/unconfigured PyPI) and `v0.2.0a1` (prerelease, Filesystem zero-config scope, Codex MCP-only limitation, PyPI status).

- [ ] **Step 5: Commit**

```bash
git add docs/release-runbook.md README.md CHANGELOG.md SECURITY.md
git commit -m "docs: add the prerelease operator runbook"
```

### Task 6: Verify and merge the E23 PR

- [ ] **Step 1: Run every local gate**

Run: `ruff check . && mypy belay && py -3.13 -m pytest && py -3.13 -W error::ResourceWarning -m pytest -m "" --no-cov && py -3.13 scripts/traceability.py --check && py -3.13 -m conformance.cli run --target belay --level 3 && py -3.13 examples/demo.py --oops`

Expected: all pass; branch coverage ≥81%; no unclosed SQLite warning; demo verifies chain/coherence/rewind.

- [ ] **Step 2: Push and open PR E23**

Branch from merged E22 `main`. Link E23, enumerate tests and artifact matrix, embed the demo preview, and state PyPI remains disabled unless independently configured.

- [ ] **Step 3: Wait for exact required checks**

Require `test (3.12)`, `test (3.13)`, `wheel-smoke`, all three `cross-platform-clean-room (...)`, and all three `build-binaries (...)` contexts on the head SHA.

- [ ] **Step 4: Merge and update local main**

Merge only after checks pass, then fast-forward local `main` to `origin/main` and verify a clean worktree.

### Task 7: Enable the documented private security channel

- [ ] **Step 1: Enable private vulnerability reporting**

Run: `gh api --method PUT repos/Jairogelpi/belay-mcp/private-vulnerability-reporting`

- [ ] **Step 2: Read it back**

Run: `gh api repos/Jairogelpi/belay-mcp/private-vulnerability-reporting`

Expected: enabled/true response.

- [ ] **Step 3: Stop before branch protection**

Do not create the `main` protection rule yet. The approved rollout applies it only after the immutable prerelease and all five artifact classes have been verified.

### Task 8: Publish immutable release history

- [ ] **Step 1: Run preflight on merged main**

Run: `py -3.13 scripts/release_preflight.py prepare --tag v0.2.0a1 --repo Jairogelpi/belay-mcp`

Expected: clean tree, main equals origin, versions match, tag absent, PyPI disabled/absent, required checks green.

- [ ] **Step 2: Create the historical `v0.1.0` GitHub prerelease note**

Attach notes to the existing tag without moving it. Read the tag SHA before and after and assert equality.

- [ ] **Step 3: Create and push `v0.2.0a1` once**

Create the tag at the verified merged-main SHA and push it. Never delete, force-update, or recreate it.

- [ ] **Step 4: Monitor the release workflow**

Wait for all jobs. If it fails, follow the documented default-branch `workflow_dispatch` recovery; do not move the tag.

- [ ] **Step 5: Verify the GitHub prerelease**

Download assets to a temporary directory and run `release_preflight.py verify-artifacts`. Verify prerelease flag, tag SHA, five artifact classes, and current README/CHANGELOG links.

- [ ] **Step 6: Record final evidence**

Preserve PR URLs, merged SHAs, CI/release run URLs, private-reporting readback, tag SHAs, artifact checksums, and PyPI job status before proceeding to protection.

### Task 9: Protect `main` after release verification

- [ ] **Step 1: Create main protection**

Only after Task 8 verifies the immutable prerelease, use the GitHub rulesets/protection API with PR required, zero approvals, administrator bypass, and the exact nine required contexts proven by `required-checks`. Preserve a recoverable JSON request file outside the repository or in the task transcript; do not commit credentials.

- [ ] **Step 2: Read protection back**

Run the corresponding `gh api` GET and verify every context, pull-request rule, zero-approval setting, and administrator bypass actor.

- [ ] **Step 3: Record final evidence**

Add the protection readback to the PR/release/security/tag/artifact evidence. E23 is complete only after every external readback succeeds.
