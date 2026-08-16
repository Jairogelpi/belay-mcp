## What and why

<!-- One or two sentences: what changed, and the concrete problem it fixes
or the entrega/feature it implements. Link the entrega (docs/plan.md /
docs/plan-v2.md) or ADR if there is one. -->

## Scope check

- [ ] One PR for one entrega/feature/fix — no unrelated changes mixed in.
- [ ] If this changes behavior, `docs/spec.md` was updated in its own
      commit first (or this PR doesn't change normative behavior).
- [ ] New/changed behavior has a test written first (TDD) — test names
      describe behavior, not implementation.
- [ ] If this implements a spec MUST, the covering test has a
      `@spec("<section>")` marker so `docs/traceability.md` picks it up.
- [ ] Bug fixes include a regression test that fails before the fix.

## Verification

```
ruff check .
mypy belay
pytest                    # branch-covered fast gate
pytest -m "" --no-cov     # full suite (slow/subprocess tests too) before opening a PR
```

- [ ] All three pass locally.
- [ ] `belay-conformance run --target belay --level 3` still passes (if
      this touches proxy/hooks/contract/policy/approval/executor/ledger
      code).

## Notes for reviewers

<!-- Anything non-obvious: a P0/P1 a self-review caught, a deliberate
scope cut, a known follow-up this PR intentionally doesn't do. -->
