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
4. One pull request per entrega/feature — don't mix unrelated changes.
5. Conventional commit messages (`feat:`, `fix:`, `test:`, `docs:`, ...).

## Reporting issues

Use the issue templates: "Propose a contract pack" for new example
contracts, or "Spec ambiguity" if `docs/spec.md` is unclear or underspecified.
