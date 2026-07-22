# Belay

**Safe, reversible tool execution for AI agents.**

Belay is an MCP proxy that sits between an agent and its tool servers. It
turns "the agent can call anything" into "every tool call is declared,
previewable, gated, and — when it goes wrong — reversible."

> Status: **L1 preview** (E3 of the [implementation plan](docs/plan.md)).
> Contracts (§4), the event ledger (§9), and a real L1 MCP proxy (§3, §4.6,
> Appendix C) work end to end: `belay wrap` + `belay run` front a real
> upstream MCP server over stdio, resolving contracts, applying the default
> rule, and recording every call to the ledger. Plans, policy, approvals,
> sagas, and rewind (§5-§8, §10) land in E4-E7. The protocol is specified in
> [`docs/spec.md`](docs/spec.md) (Belay Specification 0.1).

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

## How it fits

```
Agent (LLM) ──MCP──▶ Belay ──MCP──▶ tool servers
                       │
   contracts · policy · planner · approvals · saga executor · rewind
                       │
                event ledger (append-only, hash-chained)
```

See [`docs/architecture.md`](docs/architecture.md) for the full diagram (E9)
and [`docs/spec.md`](docs/spec.md) §3 for the normative request lifecycle.

## Install

```bash
pip install belay-mcp   # not yet published — coming with v0.1.0
```

For development:

```bash
git clone https://github.com/belay-mcp/belay.git
cd belay
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

`belay wrap`/`belay run` implement L1: contract resolution, the default
rule, and passthrough execution with full ledger recording, over stdio.
Plan/policy/approval stages exist as documented no-op stubs
(`belay/proxy/lifecycle.py`) ready for E4-E6 to fill in — see
[ADR 0003](docs/adr/0003-e3-proxy-l1.md).

The full 3-minute demo — an agent attempts a bulk delete, gets paused,
a human approves a narrowed version, something still goes wrong, and
`belay rewind` restores the prior state with an honest report — is scripted
in `examples/demo.py` and described in `docs/plan.md` §10. It ships in E9.

## Roadmap

Built entrega by entrega per [`docs/plan.md`](docs/plan.md); each closes a
slice of [`docs/spec.md`](docs/spec.md):

| Entrega | Delivers | Spec sections |
|---|---|---|
| E0 | Repo scaffolding, CI, tooling | — |
| E1 | Contracts + expression language | §4 |
| E2 | Event ledger | §9 |
| E3 | L1 proxy + CLI (first publishable milestone) | §3, §4.6, App. C |
| E4 | Planner + policy engine | §5, §6 |
| E5 | Approvals | §7 |
| E6 | Saga executor | §8 |
| E7 | Rewind (closes L3 conformance) | §10 |
| E8 | Public conformance suite + example packs | §13 |
| E9 | Demo, docs, portfolio polish, v0.1.0 release | — |

## Conformance

Belay targets **L3** conformance (contracts + plans/policy/approvals +
sagas/rewind, spec §13) at v0.1.0, verified by the `belay-conformance` suite
built in E8.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md) (rules
for any human or AI agent working on this repo).

## License

MIT — see [`LICENSE`](LICENSE). The specification text
([`docs/spec.md`](docs/spec.md)) is additionally available under CC-BY-4.0.
