# E21 Quality and Release Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the supported test matrix clean and truthful by fixing the Windows shell regression, closing SQLite resources, enforcing measured branch coverage, and reconciling public documentation.

**Architecture:** Add one small SQLAlchemy engine-lifecycle abstraction and make every engine owner close it at its natural boundary; stores that create their own engine expose `close()`/context-manager behavior while shared-engine consumers never dispose a borrowed engine. Keep the Windows fix in the portable test harness and keep release truth in an ADR plus durable CI-derived wording.

**Tech Stack:** Python 3.12/3.13, SQLAlchemy 2, pytest/pytest-cov, Typer, GitHub Actions, Markdown.

---

## File structure

- Create `belay/db/lifecycle.py`: ownership-aware engine lease with explicit and finalizer-backed disposal.
- Create `tests/db/test_lifecycle.py`: focused ownership, idempotent-close, and ResourceWarning regressions.
- Create `docs/adr/0027-e21-release-truth.md`: immutable `v0.1.0` history and measured alpha criteria.
- Modify `belay/approvals/queue.py`, `belay/executor/idempotency.py`, `belay/ledger/store.py`: adopt the lifecycle API.
- Modify `belay/supervisor/server.py`, `belay/cli/main.py`: dispose shared/direct engines at process and command boundaries.
- Modify engine-producing test helpers in `tests/hooks/`, `tests/planner/`, and `tests/supervisor/`: yield and dispose borrowed engines.
- Modify `tests/tools/test_install_scripts.py`: validate Bash syntax through stdin.
- Modify `pyproject.toml`, `.github/workflows/ci.yaml`: branch coverage and the 81% non-decreasing floor.
- Modify `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `.github/pull_request_template.md`, and `docs/plan-v2.md`: remove contradictions and stale counts/timing.

### Task 1: Record release truth

**Files:**
- Create: `docs/adr/0027-e21-release-truth.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write the ADR**

Record these decisions verbatim in substance: `v0.1.0` stays immutable, contains `0.1.0.dev0`, did not publish to PyPI, and did not meet the historical 90%-branch/<60s global DoD; `v0.2.0a1` is a prerelease; the measured branch baseline is 81.15% and the enforceable floor is 81%, upward-only.

- [ ] **Step 2: Add the E21 changelog entry**

Describe Windows portability, explicit SQLite disposal, branch coverage, and corrected public claims without claiming E21 is complete yet.

- [ ] **Step 3: Check documentation formatting**

Run: `git diff --check`

Expected: exit 0.

- [ ] **Step 4: Commit**

```bash
git add docs/adr/0027-e21-release-truth.md CHANGELOG.md
git commit -m "docs: record v0.1.0 release truth"
```

### Task 2: Make the shell syntax regression portable

**Files:**
- Modify: `tests/tools/test_install_scripts.py:47-61`

- [ ] **Step 1: Preserve the current failing evidence**

Run on the WSL-shim machine: `py -3.13 -m pytest tests/tools/test_install_scripts.py::test_install_sh_has_valid_syntax -q --no-cov`

Expected before the fix: FAIL with `/bin/bash: /c/.../scripts/install.sh: No such file or directory`.

- [ ] **Step 2: Replace path translation with stdin syntax checking**

Use the resolved Bash executable but no script path:

```python
script = INSTALL_SH.read_text(encoding="utf-8")
result = subprocess.run(
    [bash, "-n"], input=script, capture_output=True, text=True, check=False
)
```

Delete `_msys_path` if no other test uses it.

- [ ] **Step 3: Run the focused test**

Run: `py -3.13 -m pytest tests/tools/test_install_scripts.py -q --no-cov`

Expected: 6 passed (or the current file total), zero failures.

- [ ] **Step 4: Commit**

```bash
git add tests/tools/test_install_scripts.py
git commit -m "test: validate install script through bash stdin"
```

### Task 3: Add an ownership-aware engine lifecycle

**Files:**
- Create: `belay/db/lifecycle.py`
- Create: `tests/db/test_lifecycle.py`
- Modify: `belay/approvals/queue.py`
- Modify: `belay/executor/idempotency.py`
- Modify: `belay/ledger/store.py`

- [ ] **Step 1: Write failing lifecycle tests**

Cover: internally created engine is disposed once by `close()`, externally supplied engine is not disposed, repeated close is safe, context-manager exit closes, and dropping an unclosed owner invokes the safety finalizer.

```python
def test_borrowed_engine_is_not_disposed() -> None:
    engine = create_engine("sqlite:///:memory:")
    lease = EngineLease.borrow(engine)
    lease.close()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("select 1").scalar_one() == 1
    engine.dispose()
```

- [ ] **Step 2: Run the new tests red**

Run: `py -3.13 -m pytest tests/db/test_lifecycle.py -q --no-cov`

Expected: FAIL because `belay.db.lifecycle.EngineLease` does not exist.

- [ ] **Step 3: Implement `EngineLease`**

The API is `EngineLease.create(db_url)`, `EngineLease.borrow(engine)`, `.engine`, `.close()`, `__enter__`, and `__exit__`. Only `create` registers a `weakref.finalize` safety net calling `Engine.dispose`; `close` invokes/detaches it exactly once. Borrowed engines are never disposed.

- [ ] **Step 4: Adopt leases in the three self-creating stores**

Each constructor creates or borrows a lease, exposes `close()` and context-manager methods, and keeps its existing public constructor signature. Do not let one store dispose a shared engine.

- [ ] **Step 5: Run focused store suites**

Run: `py -3.13 -m pytest tests/db tests/ledger/test_store.py tests/approvals/test_queue.py tests/executor/test_idempotency.py -q --no-cov`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add belay/db/lifecycle.py belay/approvals/queue.py belay/executor/idempotency.py belay/ledger/store.py tests/db/test_lifecycle.py
git commit -m "fix: manage owned SQLAlchemy engines"
```

### Task 4: Close shared engines at application and fixture boundaries

**Files:**
- Modify: `belay/supervisor/server.py`
- Modify: `belay/cli/main.py`
- Modify: `tests/hooks/test_codex_adapter.py`
- Modify: `tests/hooks/test_file_snapshot.py`
- Modify: `tests/hooks/test_gate.py`
- Modify: `tests/planner/adapters/test_sql.py`
- Modify: `tests/planner/test_planner.py`
- Modify: `tests/supervisor/test_idempotency.py`
- Test: `tests/supervisor/test_server_client.py`

- [ ] **Step 1: Write a failing supervisor disposal test**

Patch the shared engine's `dispose`, request shutdown, and assert disposal happens once after `serve_forever` exits, including listener/setup failure via `try/finally`.

- [ ] **Step 2: Run the supervisor test red**

Run: `py -3.13 -m pytest tests/supervisor/test_server_client.py -k disposes -q --no-cov`

Expected: FAIL because `Supervisor` does not retain/dispose its engine.

- [ ] **Step 3: Make `Supervisor` own the shared engine**

Store the engine on `self`, pass it as borrowed to queue/idempotency/ledger/snapshots, add `close()`, and place server shutdown in `try/finally: self.close()`.

- [ ] **Step 4: Wrap direct CLI engines in `try/finally`**

Update `hooks_list_edits`, `hooks_rewind`, `_hooks_approval_queue` callers, and `_hooks_ledger_for` callers so the command-level owner disposes after use. Do not return a store whose owner has already disposed its engine; either return `(store, engine)` to an enclosing context or add a private context manager.

- [ ] **Step 5: Convert direct test engine helpers to yield fixtures/context managers**

Every `create_engine(` occurrence under `tests/` must have a matching `engine.dispose()` in `finally`, except a test intentionally exercising `EngineLease` finalization.

- [ ] **Step 6: Run the ResourceWarning audit**

Run: `py -3.13 -W error::ResourceWarning -m pytest -m "" --no-cov`

Expected: all non-live tests pass with no unclosed-SQLite warning. Do not add a warning filter.

- [ ] **Step 7: Commit**

```bash
git add belay/supervisor/server.py belay/cli/main.py tests/hooks tests/planner tests/supervisor
git commit -m "fix: dispose shared SQLite engines"
```

### Task 5: Enforce branch coverage at the measured floor

**Files:**
- Modify: `pyproject.toml:100-122`
- Modify: `.github/workflows/ci.yaml`

- [ ] **Step 1: Make the configuration test fail**

Add an assertion in `tests/tools/test_project_config.py` that pytest addopts contains `--cov-branch` and coverage `fail_under` is at least 81.

- [ ] **Step 2: Run the config test red**

Run: `py -3.13 -m pytest tests/tools/test_project_config.py -q --no-cov`

Expected: FAIL on the missing branch flag and current floor 79.

- [ ] **Step 3: Update coverage and CI**

Add `--cov-branch` to pytest addopts, set `fail_under = 81`, and make CI name the measured fast gate clearly. Keep the full suite `--no-cov` to avoid duplicate runtime.

- [ ] **Step 4: Measure locally**

Run: `py -3.13 -m pytest`

Expected: PASS and branch-aware total ≥81%.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .github/workflows/ci.yaml tests/tools/test_project_config.py
git commit -m "ci: enforce measured branch coverage"
```

### Task 6: Reconcile every public claim

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `CONTRIBUTING.md`
- Modify: `SECURITY.md`
- Modify: `.github/pull_request_template.md`
- Modify: `docs/plan-v2.md`

- [ ] **Step 1: Remove stale facts**

Replace `834 tests`, `Seven entregas`, fixed local durations, the stale R1.7.4 statement, and the claim that the global DoD is complete. Describe E10-E20 as eleven deliveries and link CI instead of freezing counts.

- [ ] **Step 2: Correct contributor commands**

State that `pytest` is the branch-covered fast gate and `pytest -m "" --no-cov` is the full suite. Use the same commands in the PR template.

- [ ] **Step 3: Unify vulnerability instructions**

Both documents must say never publish exploit details, use GitHub private vulnerability reporting, and if unavailable open only a content-free request for a private channel.

- [ ] **Step 4: Add durable release status**

State that `v0.1.0` is historical/incomplete and `v0.2.0a1` is the next alpha; do not claim a GitHub Release or PyPI publication before E23 performs it.

- [ ] **Step 5: Search for contradictions**

Run: `rg -n "834|Seven entregas|under 30s|~85s|DoD.*complete|not built|not yet built" README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md docs .github`

Expected: no stale claim; legitimate historical quotations must be explicitly contextualized.

- [ ] **Step 6: Commit**

```bash
git add README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md .github/pull_request_template.md docs/plan-v2.md
git commit -m "docs: align public alpha status"
```

### Task 7: Verify and prepare the E21 PR

**Files:**
- Modify: none unless verification exposes a defect.

- [ ] **Step 1: Run static gates**

Run: `ruff check . && mypy belay`

Expected: both pass.

- [ ] **Step 2: Run tests and warnings gate**

Run: `py -3.13 -m pytest && py -3.13 -W error::ResourceWarning -m pytest -m "" --no-cov`

Expected: both pass; branch coverage ≥81%; no unclosed SQLite warning.

- [ ] **Step 3: Run protocol evidence gates**

Run: `py -3.13 scripts/traceability.py --check && py -3.13 -m conformance.cli run --target belay --level 3`

Expected: all MUSTs covered and L3 passes.

- [ ] **Step 4: Self-review the diff**

Run: `git diff origin/main...HEAD --check && git status --short`

Expected: clean formatting and no uncommitted files.

- [ ] **Step 5: Push and open PR E21**

The PR body must link `docs/plan.md` E21, list tests added, paste the exact verification summaries, and avoid claiming E22/E23.
