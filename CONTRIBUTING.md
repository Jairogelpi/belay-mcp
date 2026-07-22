# Contributing to Belay

Belay is built spec-first: `docs/spec.md` (the Belay Specification 0.1) is
the source of truth, and `docs/plan.md` tracks the implementation roadmap.

## Ground rules

- **Spec drives code.** If behavior isn't in `docs/spec.md`, don't invent it
  silently — propose a spec change in its own commit/PR first.
- **TDD.** No production code without a failing test first. Test names
  describe behavior (`test_refuses_destructive_tool_without_contract`).
- **No `eval`/`exec`.** The contract expression language (spec §4.3) is
  parsed with a closed grammar, never evaluated as code.
- **No LLM calls inside `belay/`.** The safety path is fully deterministic.
- **English for public artifacts.** Code, comments, commit messages, README,
  and CHANGELOG are English. ADRs and working notes may be Spanish.

## Workflow

1. Fork and branch from `main`.
2. `pip install -e ".[dev]"`.
3. `ruff check . && mypy belay && pytest` must pass before you open a PR.
   The full suite currently runs in ~85s; `pytest -m "not slow"` runs the
   fast loop (unit/property/in-memory integration, no real subprocess) in
   under 30s for quick local iteration — run the full suite (including
   `@pytest.mark.slow` tests that spawn real `belay run` subprocesses over
   stdio) before opening a PR.
4. One pull request per entrega/feature — don't mix unrelated changes.
5. Conventional commit messages (`feat:`, `fix:`, `test:`, `docs:`, ...).
6. New behavior needs a test first (TDD, see above) and, if it implements a
   spec MUST, a `@spec("<section>")` marker so `docs/traceability.md`'s
   generator can find it.

## Bug fixes

Every bug fix starts with a regression test that fails before your fix and
passes after — no fix without one (see AGENTS.md).

## Reporting issues

Use the issue templates: ["Propose a contract
pack"](.github/ISSUE_TEMPLATE/propose-contract-pack.yaml) for new example
contracts (filesystem/CRM/email-style packs live in `examples/contracts/`),
or ["Spec ambiguity"](.github/ISSUE_TEMPLATE/spec-ambiguity.yaml) if
`docs/spec.md` is unclear, underspecified, or internally inconsistent. Spec
changes always land in their own commit/PR with a decision note — never
silently diverged from in code (see `AGENTS.md` rule 1).

## Security

Belay's safety path (contracts, planner, policy, approvals, executor,
rewind, ledger — everything under `belay/`, excluding `belay/cli`) is
deterministic: no `eval`/`exec`, no LLM calls, no network calls beyond MCP
to configured tool servers. If you find a way to bypass a policy verdict,
forge a ledger entry, or get the expression language (spec §4.3) to
execute arbitrary code, please open an issue rather than a PR with the
exploit inline, so it can be triaged first.
