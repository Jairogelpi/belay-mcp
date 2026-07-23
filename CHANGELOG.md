# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches 1.0.

## [Unreleased]

### Added

- **Statistical anomaly baselines (E10, plan-v2 §"E10"):** `belay/policy/baseline.py`
  -- deterministic, no-LLM, no-network per-session rolling mean/stddev
  (Welford's algorithm) computed from the ledger's own `plan_created`
  history. New `anomaly` policy dimension in `PolicyEngine.evaluate`,
  combined with `tools`/`quiet_hours`/irreversible-default by the same
  max-severity rule. Zero manual configuration required (`min_samples=10`,
  `z_score_threshold=3.0`, `verdict=pause` by default); cold-start below
  `min_samples` never blocks. `examples/demo_anomaly.py`,
  `docs/adr/0010-e10-anomaly-baselines.md`.

## [0.1.0] - 2026-07-22

First feature-complete release: an MCP proxy giving agents contract-based,
policy-gated, reversible tool execution, L3 conformant against
`docs/spec.md` (Belay Specification 0.1). Built entrega-by-entrega (E0-E9)
per `docs/plan.md`; see `docs/adr/` for the decision record of each.

### Added

- **Scaffolding (E0):** package layout, `pyproject.toml`, ruff/mypy/pytest
  configuration, pre-commit hooks, GitHub Actions CI, Alembic migrations.
- **Contracts + expression language (E1, spec §4):** `belay/contracts` —
  `parse`/`evaluate` for the closed-grammar expression language (no
  `eval`/`exec`), YAML/JSON contract loading with JSON-Schema validation,
  canonical JSON + `set_hash`.
- **Event ledger (E2, spec §9):** `belay/ledger` — append-only, hash-chained
  events, chain + coherence verification, deterministic replay, secret
  redaction. `belay verify`.
- **L1 MCP proxy + CLI (E3, spec §3, §4.6, App. C):** `belay/proxy`,
  `belay wrap` / `belay run`. Contract resolution, the default rule for
  tools without a contract, passthrough execution, full ledger recording
  over stdio against any standard MCP client.
- **Planner + policy engine (E4, spec §5, §6):** `belay/planner`,
  `belay/policy` — dry-run effect estimation (`contract` and
  `native_dry_run` adapters), blast-radius caps, `deny > pause > allow`
  verdicts, plan expiration. `belay plan`.
- **Approvals (E5, spec §7):** `belay/approvals` — pending/approved/
  rejected/expired lifecycle, structural no-self-approval (no agent-facing
  approval surface), approver binding to `plan_id`. `belay approvals
  list|approve|reject`.
- **Saga executor (E6, spec §8):** `belay/executor` — the normative
  journaled/capturing/calling/result_recorded/compensation_registered/
  committed step cycle, idempotency keys, crash recovery from the ledger
  alone, conditional-undo re-checking.
- **Rewind (E7, spec §10):** `belay/rewind` — reverse-order compensation,
  session fencing across processes, honest `fully_rewound` reporting,
  `--dry-run` and `--skip-and-continue`. `belay rewind`. Closes L3
  conformance.
- **Public conformance suite (E8, spec §13):** `belay-conformance` — a
  target-agnostic pytest suite (`@conformance(level=...)`) driven by a
  ~6-method `ConformanceTarget` adapter, plus example contract packs
  (filesystem, CRM, email/irreversible).
- **Demo, docs, and portfolio polish (E9):** `examples/demo.py` (real,
  runnable reproduction of the `docs/plan.md` §10 scenario, `--oops`
  variant included), `docs/architecture.md` (full Mermaid component +
  lifecycle diagram), README badges/quickstart/comparison section,
  `CONTRIBUTING.md` + issue templates, `.github/workflows/release.yaml`
  (PyPI trusted publishing on tag push).

### Known gaps (tracked, not blocking v0.1.0)

- `belay approvals approve --narrow <filter>` is not implemented as CLI
  surface; the tested equivalent is re-planning with narrower args (spec
  §12, new `plan_id`) and approving that plan instead. See
  [ADR 0007](docs/adr/0007-e7-rewind.md), [ADR 0009](docs/adr/0009-e9-demo-docs-polish.md).
- `docs/traceability.md` (the spec-section -> test generator described in
  `docs/plan.md` §8) was not built in any entrega; spec MUST coverage is
  currently verified by reading the test suite, not a generated table.
- No PyPI release exists yet; publishing requires the maintainer to
  configure trusted publishing on PyPI first, then push the `v0.1.0` tag.
- No demo GIF/asciinema recording is embedded in the README; `asciinema`
  and `vhs` were not available in the sandbox this entrega was built in. A
  VHS tape script (`examples/demo.tape`) is checked in for the maintainer
  to render.
- The default `pytest` run (fast loop, `slow`-marked subprocess/integration
  tests deselected) finishes in ~25-30s but covers `belay/` at ~89%, just
  under the §0 90% bar; the full suite (CI's second `pytest` step, `-m ""`)
  covers ~93% but takes ~85-90s, over the §0 60s bar. The two §0 criteria
  are in tension as specified; CI runs both the fast loop and the full
  suite so neither speed nor coverage is silently dropped, but no single
  `pytest` invocation satisfies both numbers at once.
