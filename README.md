# Belay

[![CI](https://github.com/Jairogelpi/belay-mcp/actions/workflows/ci.yaml/badge.svg)](https://github.com/Jairogelpi/belay-mcp/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Conformance: L3](https://img.shields.io/badge/conformance-L3-brightgreen.svg)](conformance)

> PyPI badge intentionally omitted: `belay-mcp` is not published yet (see
> "Release status" below) — a badge pointing at a nonexistent PyPI project
> would 404, so it's left out rather than faked.

**Safe, reversible tool execution for AI agents.**

Belay is an MCP proxy that sits between an agent and its tool servers. It
turns "the agent can call anything" into "every tool call is declared,
previewable, gated, and — when it goes wrong — reversible."

> Status: **`v0.1.0` tagged, L3 conformance.** E0-E9 (`docs/plan.md`) shipped
> the full lifecycle — contracts (§4), ledger (§9), the L1 proxy (§3, §4.6,
> Appendix C), planner + policy (§5, §6), approvals (§7), the saga executor
> (§8), and rewind (§10), diagrammed in
> [`docs/architecture.md`](docs/architecture.md). Seven further entregas
> (E10-E16, `docs/plan-v2.md`) shipped past v0.1.0 without breaking L3 — see
> "What's new since v0.1.0" below. 307 tests, ≥90% coverage on the full run,
> [`docs/traceability.md`](docs/traceability.md) proving every normative MUST
> in the spec has a real test (CI-enforced, not a stale doc). The protocol is
> specified in [`docs/spec.md`](docs/spec.md) (Belay Specification 0.1).

## Why

Agents that can delete, spend, or send are one bad plan away from an
incident. Belay's answer isn't "trust the model more" — it's infrastructure:

- **Contracts** (spec §4) declare, per tool, whether an action is
  `reversible`, `irreversible`, or `conditional`, and — if reversible — what
  the concrete undo call is.
- **Dry-run planning** (spec §5) predicts effects (`create`, `update`,
  `delete`, `send`, `spend`, ...) before anything executes, honestly marking
  what's estimated versus exact.
- **Policy** (spec §6) enforces blast-radius caps (row counts, spend limits,
  recipient counts, quiet hours) and picks the most restrictive verdict.
- **Human approval** (spec §7) parks anything the policy pauses, with
  no-self-approval enforced structurally — an agent cannot approve its own
  action through any surface Belay exposes.
- **Saga execution** (spec §8) commits actions as staged steps and
  materializes each compensation at commit time, so undo never re-evaluates
  live state.
- **Rewind** (spec §10) runs compensations in reverse order and reports
  honestly what was undone, what's irreversible, and what's indeterminate —
  it never claims "fully rewound" unless that's true.
- **An append-only, hash-chained ledger** (spec §9) makes every decision and
  every tool call independently verifiable and replayable.

No LLM sits on the safety path. Belay is deterministic end to end.

## What's new since v0.1.0

Seven entregas landed on top of the v0.1.0 lifecycle (`docs/plan-v2.md`,
ADRs 0010-0018), each additive — none weakened an existing test or broke L3
conformance:

- **Statistical anomaly baselines** (E10) — a per-session rolling
  mean/stddev (Welford's algorithm, no LLM, no manual threshold) pauses an
  action that's wildly outside its own session's normal pattern, even with
  zero `Cap` configured for that tool. `examples/demo_anomaly.py`.
- **Real SQL dry-run** (E11) — instead of a declared estimate, a
  `BEGIN ... ROLLBACK` against the actual database reports the real
  affected-row count before a human approves anything, never committing.
  `examples/demo_sql.py`.
- **Counterfactual replay** (E12) — `belay counterfactual <session> --at-step
  N --override '{"verdict":"deny"}'` answers "what would have happened if a
  human had decided differently here" entirely offline, from the ledger
  alone — never calling the real upstream, never touching the real
  session's chain. Honest by construction: it only ever reports
  `unchanged`, `diverged` (with the real basis), or `unknown` — never a
  fabricated concrete outcome. `examples/demo_counterfactual.py`.
- **Signed, offline-verifiable evidence** (E13) — `belay verify-export` +
  `belay verify-evidence` produce and check an Ed25519-signed bundle that
  needs nothing but the file itself and a public key: no `belay.db`, no
  network. Tamper detection is precise (chain vs. signature vs. summary
  mismatch), not a single opaque pass/fail. `examples/demo_signed_evidence.py`.
- **Identity attribution** (E14) — every session is bound to an explicit
  `--initiated-by` (and optional `--on-behalf-of`) identity, folded into
  E13's signature so forging *who* triggered a session is caught exactly
  like tampering with the ledger. `examples/demo_attribution.py`.
- **Per-identity irreversible-action quota** (E15) — beyond E4's per-call
  caps, a rolling window limits how many irreversible actions one identity
  can accumulate, so "I approved this once" can't silently become "the
  agent did it 200 times." `examples/demo_quota.py`.
- **Blast-radius self-explanation** (E16) — the governed response back to
  the *agent itself* (not just the human's CLI) carries a structured,
  template-filled explanation of why a call paused/was denied, with a
  deterministic `suggested_action` when one mechanically applies. In
  `examples/demo_self_explain.py` the agent reads its own explanation,
  narrows its request, and gets `allow` — with zero human approval step.

## How it fits

```
Agent (LLM) ──MCP──▶ Belay ──MCP──▶ tool servers
                       │
   contracts · policy · planner · approvals · saga executor · rewind
                       │
                event ledger (append-only, hash-chained)
```

See [`docs/architecture.md`](docs/architecture.md) for the full diagram and
[`docs/spec.md`](docs/spec.md) §3 for the normative request lifecycle.

## Install

```bash
pip install belay-mcp   # not yet published to PyPI — see "Release status" below
```

For development:

```bash
git clone https://github.com/Jairogelpi/belay-mcp.git
cd belay-mcp
pip install -e ".[dev]"
pytest
```

## Quickstart

```bash
belay wrap examples/fs-server --contracts examples/contracts/fs.yaml
belay run &
# any standard MCP client now talks to Belay instead of fs-server directly:
# tools with a contract or readOnlyHint pass through, everything else is
# refused with contract_missing (spec §4.6) — logged to belay.db either way.
belay verify belay.db
```

Every command above was re-run against a clean checkout while writing this
README; `belay verify belay.db` prints `chain: OK` / `coherence: OK` on an
empty, freshly-wrapped ledger.

## Demo

The 3-minute portfolio demo (spec-driven scenario in `docs/plan.md` §10) is
a real, runnable script — not a mock:

```bash
python examples/demo.py         # bulk delete -> pause -> narrow -> approve -> execute -> rewind
python examples/demo.py --oops  # same, plus a wrong-filter mistake that rewind then undoes
```

It shells out to the real `belay` CLI (`wrap`, `approvals list/approve`,
`rewind --dry-run`, `rewind --by`) and drives a real MCP session against
`examples/crm-mock`, ending in `chain: OK` / `coherence: OK` and "session
fully compensated" — genuine output, generated live each run.

`belay approvals approve --narrow <filter>` does not exist as CLI surface
(documented gap, see [ADR 0007](docs/adr/0007-e7-rewind.md) and
[ADR 0009](docs/adr/0009-e9-demo-docs-polish.md)); the demo's "narrowing"
step is the equivalent E7 actually built and tested — the agent retries with
a different, narrower filter, which is a new plan the human approves
instead of the original one.

**Recording:** a VHS tape script (`examples/demo.tape`) is checked in for
whoever has the `vhs` binary to render a GIF from — `asciinema` and `vhs`
were not available in the sandbox this entrega was built in, so no
recording is embedded here yet. This is an honest gap, not a placeholder
GIF; see ADR 0009.

## Roadmap

Built entrega by entrega per [`docs/plan.md`](docs/plan.md); each closes a
slice of [`docs/spec.md`](docs/spec.md):

| Entrega | Delivers | Spec sections | Status |
|---|---|---|---|
| E0 | Repo scaffolding, CI, tooling | — | done |
| E1 | Contracts + expression language | §4 | done |
| E2 | Event ledger | §9 | done |
| E3 | L1 proxy + CLI (first publishable milestone) | §3, §4.6, App. C | done |
| E4 | Planner + policy engine | §5, §6 | done |
| E5 | Approvals | §7 | done |
| E6 | Saga executor | §8 | done |
| E7 | Rewind (closes L3 conformance) | §10 | done |
| E8 | Public conformance suite + example packs | §13 | done |
| E9 | Demo, docs, portfolio polish, v0.1.0 release | — | done (tag/PyPI pending, see below) |
| E10 | Statistical anomaly baselines | — (plan-v2) | done |
| E11 | Real SQL dry-run adapter | §5.3 (extended) | done |
| E12 | Counterfactual replay | §9.4 (extended) | done |
| E13 | Signed, offline-verifiable evidence | §9 (extended) | done |
| E14 | Identity attribution | §9, §12 (extended) | done |
| E15 | Per-identity irreversible-action quota | §6 (extended) | done |
| E16 | Blast-radius self-explanation | §6, §7 (extended) | done |
| E17 | `docs/traceability.md` generator, CI-enforced | §8 (plan.md) | done |

## Conformance

Belay is **L3** conformant (contracts + plans/policy/approvals +
sagas/rewind, spec §13), verified by the `belay-conformance` suite:

```bash
belay-conformance run --target belay --level 3
```

`belay-conformance` is a separate, target-agnostic package: any MCP proxy
that implements the ~6-method `ConformanceTarget` adapter can run the same
suite against itself.

Beyond conformance, [`docs/traceability.md`](docs/traceability.md) proves the
narrower claim that *every normative MUST in `docs/spec.md` has at least one
real, named test* — a hand-curated list of MUSTs cross-referenced against
`@spec("X.Y")` markers on test functions, generated by
`scripts/traceability.py`. CI runs the generator and fails the build if any
MUST is left uncovered, so the claim can't silently rot into a stale doc.

## How Belay compares

Belay isn't a gateway, an observability tool, or an enterprise workflow
engine — it overlaps with pieces of each without being a drop-in
replacement for any:

- **MCP gateways/routers** (e.g. [mcp-gateway](https://github.com/lasso-security/mcp-gateway),
  various vendor "MCP proxy" products) focus on auth, rate limiting, and
  routing across multiple MCP servers. Belay assumes you already have (or
  don't need) that layer and adds contract-based reversibility on top —
  its concern is "what happens if this specific call was a mistake",
  not multiplexing.
- **Observability/tracing for agents** (e.g. [LangSmith](https://www.langchain.com/langsmith),
  [Langfuse](https://langfuse.com/)) record what an agent did after the
  fact. Belay's ledger (spec §9) is adjacent but exists to make actions
  *governable and undoable*, not to analyze prompts/latency/cost.
- **Enterprise workflow/saga engines** (e.g. [Temporal](https://temporal.io/),
  [AWS Step Functions](https://aws.amazon.com/step-functions/)) implement
  the saga pattern generally, for developer-authored workflows. Belay
  narrowly targets one thing: an *agent-invoked* MCP tool call, undone via
  a contract the tool integrator declares once — it is not a general
  orchestration engine and doesn't try to be.

## Release status

`v0.1.0` is tagged (`git tag v0.1.0`, on top of the full E0-E9 Definition of
Done, §0) but **not published to PyPI yet** — that's a manual step for the
maintainer (PyPI trusted publishing must be configured on the PyPI project
first; an agent cannot do that). `main` is currently ahead of the `v0.1.0`
tag with E10-E17 (see "What's new since v0.1.0" above); no new tag has been
cut for those yet. See
[`.github/workflows/release.yaml`](.github/workflows/release.yaml) and
[`CHANGELOG.md`](CHANGELOG.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md) (rules
for any human or AI agent working on this repo).

## License

MIT — see [`LICENSE`](LICENSE). The specification text
([`docs/spec.md`](docs/spec.md)) is additionally available under CC-BY-4.0.
