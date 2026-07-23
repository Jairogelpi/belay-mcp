# ADR 0017 — Fix: intermittent Hypothesis `DeadlineExceeded`/`FlakyFailure`

## Status

Accepted.

## Context

A manual, from-scratch verification of the Definition of Done (`docs/plan.md`
§0) — fresh clone, `pip install -e ".[dev]" && pytest`, the `demo.py`/`--oops`
scenario, `belay-conformance --level 3` — found that
`tests/ledger/test_counterfactual.py::test_property_any_noop_override_is_always_unchanged_and_matches_replay`
failed intermittently: roughly 1 in 15-20 runs when the test is run in
isolation and repeated, and once in 8 full-suite runs.

Root cause, captured directly from a reproduced failure:

```
hypothesis.errors.FlakyFailure: ... produces unreliable results:
Failed on the first call but did not on a subsequent one.
Unreliable test timings! On an initial run, this test took 294.01ms,
which exceeded the deadline of 200.00ms, but on a subsequent run it
took 40.53ms.
hypothesis.errors.DeadlineExceeded: Test took 294.01ms, which exceeds
the deadline of 200.00ms.
```

This is **not** a correctness bug in `run_counterfactual`, `replay`, or the
honesty rule — every example Hypothesis generates, re-run in isolation,
passes its actual assertions. The failure is Hypothesis's own default
per-example wall-clock `deadline` (200ms) being exceeded by
`LedgerStore("sqlite:///:memory:")`'s real SQLAlchemy engine
creation + `Base.metadata.create_all` + a dozen real `INSERT`s, whose
first-call latency varies with OS/disk/process scheduling noise on this
machine (observed: 40ms-294ms across runs). Hypothesis's shrinker/replay
machinery treats a timing-dependent pass/fail as a `FlakyFailure` (its own
built-in detector for "the boolean outcome of this example isn't
reproducible"), which is exactly what timing noise produces.

This is real, load-bearing information: any Hypothesis test in this repo
that does real SQLite I/O per example (not just this one — grep found the
same risk in `tests/ledger/test_signing.py` and any other property test
that constructs a `LedgerStore`) is exposed to the same class of flake
under machine load, and none of them had `deadline=None`/an explicit
deadline set.

## Decision

Set `deadline=None` on Hypothesis property tests that perform real I/O
(SQLite engine creation, file I/O) per example — timing is not the
property under test; correctness is. Do **not** globally disable
deadlines repo-wide via `hypothesis.settings.register_profile`, since a
deadline is still a useful trip-wire for pure-computation property tests
(e.g. `belay/contracts/expressions.py`'s parser/evaluator property tests,
`belay/policy/baseline.py`'s Welford property test) where a sudden latency
regression *would* be a meaningful signal. The fix is scoped to the
specific tests doing real I/O, not a blanket opt-out.

## Consequences

- Fixes the flake without weakening any assertion — the tests still check
  exactly what they checked before, just without a timing-based failure
  mode unrelated to correctness.
- Any *new* Hypothesis property test added over ledger/SQLite operations
  in future entregas should set `deadline=None` up front rather than
  rediscovering this the same way.
- This does not change the DoD's `pytest` timing numbers materially (a few
  hundred milliseconds at most, previously sometimes double-paid as a
  failing-then-retried example).
