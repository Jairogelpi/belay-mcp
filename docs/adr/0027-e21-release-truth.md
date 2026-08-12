# ADR 0027: E21 release truth and measured quality gate

## Status

Accepted.

## Context

The public `v0.1.0` tag is historical release evidence. Its contents and the
outcome of its release workflow do not match claims that previously appeared in
public project material:

- The tag contains package version `0.1.0.dev0`.
- Its PyPI Trusted Publishing workflow failed because Trusted Publishing was
  not configured, so it did not publish to PyPI.
- It did not meet the historical global Definition of Done: at least 90% branch
  coverage and a clean-clone test run below 60 seconds.

On 2026-08-12, Python 3.13 measured the branch-aware fast gate at 81.15% total
coverage. The safe reproducible branch-aware fast-gate command is
`py -3.13 -m pytest -m "not slow and not live_conformance" --cov=belay
--cov-branch --cov-report=term`. This is a measured baseline for the current
release-readiness work, not evidence that either historical target was
achieved.

## Decision

- `v0.1.0` remains immutable. It will not be moved, recreated, or force-updated
  to alter its historical contents or publication result.
- Public release records state that `v0.1.0` contains `0.1.0.dev0`, did not
  publish to PyPI, and did not satisfy the historical 90%-branch/<60-second
  global Definition of Done.
- `v0.2.0a1` is the next GitHub prerelease.
- The enforceable branch-coverage floor is 81%, based on the 81.15% baseline
  measured on 2026-08-12. This floor is upward-only: a future change may raise
  it but may not lower it.
- Both historical targets remain explicit, unweakened, and unclaimed debt:
  90% branch coverage and a clean-clone test run below 60 seconds. Neither is
  silently lowered, and neither is declared complete retroactively.

## Consequences

Release-readiness work can enforce a truthful, measured quality floor while
preserving the stronger historical target as open debt. Public documentation
must distinguish the immutable historical tag from the planned `v0.2.0a1`
GitHub prerelease and must not describe an unconfigured or failed PyPI
publication as successful.
