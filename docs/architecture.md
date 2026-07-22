# Architecture

Stub for E0. The full diagram and component walkthrough are written in E9
once the proxy, planner, policy engine, approvals, executor, and rewind
service all exist end to end.

Planned shape (spec §3):

```mermaid
flowchart LR
    Agent -- MCP --> Belay
    Belay -- MCP --> Tools[Tool servers]
    subgraph Belay
        Contracts[Contract registry]
        Policy[Policy engine]
        Planner[Planner / dry-run]
        Approvals[Approval router]
        Executor[Saga executor]
        Rewind[Rewind service]
        Ledger[(Event ledger)]
    end
```

See `docs/spec.md` §3 for the normative request lifecycle.
