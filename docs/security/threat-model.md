# Belay — Threat Model

This document describes what Belay's Native Agent Gate and MCP proxy
actually protect against, grounded in the real code
(`belay/supervisor/`, `belay/hooks/`, `belay/approvals/`, `belay/ledger/`),
not a generic checklist. It complements
[`docs/spec.md`](../spec.md)'s §12 "Security considerations" (the
normative, protocol-level requirements) with the concrete, implementation-
level picture: what's actually deployed on a developer's machine, and where
its guarantees stop.

## Trust boundaries

```
             same OS user, same machine
┌─────────────────────────────────────────────────────┐
│  Agent (LLM) / its host (Claude Code, Codex, ...)    │
│         │ Bash, Edit/Write, native mcp__*            │
│         ▼                                            │
│  Host adapter (belay/hooks/*_adapter.py)              │
│         │ HookEvent (normalized)                      │
│         ▼                                            │
│  belay/hooks/gate.py  ── decision ──▶  ApprovalQueue  │
│         │                                    ▲        │
│         ▼                                    │        │
│  Local supervisor (belay/supervisor/) ───────┘        │
│    - authenticated IPC (named pipe / Unix socket)     │
│    - private capability token, private SQLite         │
└─────────────────────────────────────────────────────┘
                       │
                       ▼
          Human operator (`belay approvals`/`belay hooks approvals`)
```

The MCP proxy path (`belay run`/`belay wrap`) is a separate process
boundary: agent → Belay proxy → real tool server, with Belay enforcing
contracts (`belay/proxy/lifecycle.py`) before any call reaches the
upstream. Both paths write to the same `LedgerStore`/`ApprovalQueue`
storage layer (see the "MCP proxy vs. Native Agent Gate" section below for
where their guarantees actually diverge).

## Assets

- **The supervisor's capability token** (`belay_home()/keys/<install_id>.key`,
  `belay/supervisor/addressing.py`) — an HMAC key used in the
  `multiprocessing.connection` challenge-response handshake. Anyone who
  reads this file can talk to the supervisor as if they were an authorized
  hook client.
- **The approvals/idempotency SQLite database**
  (`belay_home()/data/<install_id>.db`) — the authoritative record of what's
  pending, approved, or rejected. Direct write access here is equivalent to
  forging a human's approval.
- **The ledger** (`belay.db` for the MCP path, the hooks database for the
  Native Agent Gate path) — the hash-chained event log (spec §9). Its
  integrity is what makes `belay verify` meaningful.
- **Ed25519 signing keys** (`belay keygen`, E13/E19.6) — used for
  offline-verifiable evidence bundles and release signing. A leaked private
  key lets an attacker forge evidence or a "signed" release.

## What Belay protects against

- **An agent silently causing an irreversible action.** Every MCP-proxied
  call is contract-checked, planned, and policy-evaluated before execution
  (`belay/proxy/lifecycle.py`); every Bash/file-edit/native-MCP call under
  the Native Agent Gate is classified or captured before it's allowed
  (`belay/hooks/gate.py`).
- **An agent approving its own action.** No code path in `belay/proxy` or
  `belay/hooks` can transition an `ApprovalQueue` item to `approved` — that
  capability exists only in `belay/cli/main.py`'s `approvals`/`hooks
  approvals` subcommands, which require a human to actually type a command
  (spec §12: "an agent MUST NOT be able to approve its own actions through
  any tool Belay exposes").
- **Silent tampering with the approval/ledger record.** The ledger is
  hash-chained (spec §9.2); `belay verify` recomputes the chain and detects
  a corrupted or reordered event. Approvals are durable, SQLite-backed
  state, not in-memory.
- **An ordinary allowlisted command reading Belay's own secrets.**
  `belay/hooks/gate.py::_touches_belay_home` denies even an otherwise-safe
  command if any of its path arguments resolve into `belay_home()` — so a
  plain `cat ~/.belay/keys/<id>.key` is refused even though `cat` itself is
  allowlisted.
- **A supervisor that can't be reached, or won't answer.** The gate fails
  closed: an unreachable supervisor or a timed-out request is `deny`, never
  `allow` and never a hang (`belay/supervisor/server.py`). A connected-but-
  silent client (local Slowloris) is actively timed out rather than left to
  block the worker pool.
- **A replayed or duplicated tool-call event.** Event IDs are deduplicated
  durably (SQLite, survives a supervisor restart); a reused ID with
  different content is treated as a collision and denied.
- **An approval granted for one context silently covering another.**
  Approvals are bound to the full context they were requested in — host,
  session, tool, command/args, cwd, and the repository's real git HEAD
  (not just the command string) — so approving a command in one
  repo/branch/session never approves the same string elsewhere.

## What Belay explicitly does NOT protect against

- **An arbitrary process running as the same OS user as the agent.** The
  Native Agent Gate's authentication (HMAC over a named pipe/Unix socket)
  establishes that a peer holds the capability token — it does not, and
  cannot, defend against another process owned by the same user that
  simply reads the token file directly off disk (referenced in code as
  "TRUTH-010" — see the note on unformalized invariants below). This is a
  stated limitation of the T0/T1/T2 trust-tier model, not an oversight.
- **A malicious or compromised host binary.** If Claude Code, Codex, or
  OpenCode's own binary is compromised, it can misreport what it's about
  to do to the hook adapter. `trust_tier: "T1"` (Claude Code's Bash surface
  only, per `tests/hooks/test_live_conformance.py`) certifies that a
  *denied* command's effect genuinely doesn't happen on disk for a
  correctly-behaving installed binary — it is not a guarantee against a
  trojanned one.
- **Prompt injection as an authorization mechanism.** Per spec §12, model
  output is untrusted input; nothing an agent says can approve, relax
  policy, or edit contracts. Belay does not attempt to detect or filter
  prompt injection itself — that's explicitly out of scope (spec §1), left
  to model-level guardrails.
- **A malicious contract.** Contracts are hash-pinned per session (spec
  §4.7) and MAY be signed, but Belay does not itself run a supply-chain
  review of a contract's authorship. Loading an attacker-authored contract
  that lies about a tool's reversibility is out of scope for the runtime to
  detect; this is a review/provenance problem (spec §12 "Contract supply
  chain").
- **OS-level compromise, kernel exploits, or physical access.** Belay
  assumes the OS's own process/file permission boundaries hold. It has no
  answer for a rooted machine, a compromised kernel, or physical access to
  the disk.
- **Code-signing/notarization of released binaries.** `belay release
  sign`/`verify` (E19.6) is authenticity signing (Ed25519) proving a bundle
  came from whoever holds the private key — it is not Windows
  Authenticode or Apple notarization, which need a paid, identity-verified
  certificate this project doesn't have. Windows SmartScreen/macOS
  Gatekeeper will still warn on the raw binaries regardless of this
  signature.
- **Hosts/surfaces still reporting `UNKNOWN` trust tier.** Only Claude
  Code's Bash surface has an earned `T1` (a real pinned-version conformance
  suite passed against it). Edit/Write/native-MCP surfaces, and the Codex/
  OpenCode adapters, are normalize/render-only and have not been verified
  end-to-end against a live session — treat them as unverified until they
  are. See [`docs/adapter-compatibility.md`](../adapter-compatibility.md)
  for the full per-host, per-surface matrix.

## MCP proxy vs. Native Agent Gate: known divergence

These are **not** the same governance engine. The MCP proxy
(`belay/proxy/lifecycle.py`) enforces declared `Contract`/`effects` through
a real `PolicyEngine`, with intent-contract enforcement, per-identity
quotas, anomaly baselines, and session fencing on every call. The Native
Agent Gate (`belay/hooks/gate.py`) has none of these: Bash is still
governed by a static pattern classifier (no `PolicyEngine`). An approval
granted on one path cannot satisfy the other, even for what a human would
call the same action.

**Two gaps have been closed, opt-in** ([ADR 0021](../adr/0021-r1-native-gate-contract-check.md),
R1's first slices): `belay hooks install --contracts <file>` makes native
`Edit`/`Write`/`NotebookEdit` calls resolve against a real `ContractSet`
the same way the MCP proxy's `resolve()` does -- no matching contract now
denies (`contract_missing`), instead of the old unconditional allow. The
same file also reaches native `mcp__server__tool` calls: a declared,
all-read contract now auto-allows (matching the proxy's own `readOnlyHint`
rule) instead of pausing unconditionally -- everything else still pauses
exactly as before, so this only ever narrows the default, never widens
it. Both are off by default; every install that doesn't pass
`--contracts` is unchanged. Bash is untouched by these slices and remains
fully divergent from the proxy path. Until the rest is resolved (tracked
as open R1 scope), treat the Native Agent Gate as a **materially
weaker, best-effort** governance layer compared to the MCP proxy —
appropriate for routine coding-session safety net, not a substitute for
wrapping a tool server through `belay run` when the stakes are high.

## A note on unformalized invariants

Code across `belay/hooks/`, `belay/supervisor/`, `belay/db/models.py`, and
`belay/cli/main.py` uses identifiers like `TRUTH-004`, `TRUTH-010`,
`ARCH-00X`, and `FILE-00X`. These used to be cited as if `docs/spec.md`
defined them (it doesn't — verified line-by-line, see
[ADR 0020](../adr/0020-extended-requirement-catalog.md) for the full
account); every such citation has now been corrected to stop claiming
spec.md as the source. ADR 0020 is their real, honest home: a catalog of
what each ID actually means, transcribed from the code that introduced it.

What ADR 0020 does **not** do is decide whether these should become real,
RFC-2119 normative text inside `docs/spec.md` itself (which would also mean
teaching `scripts/traceability.py`'s CI-enforced MUST-coverage check about
them, since it currently has no knowledge of these IDs at all). That
remains an open decision for whoever owns the spec, not resolved here.

## Reporting a gap in this model

If you find a way to violate one of the "protects against" guarantees
above, or a case where an "explicitly does not protect against" limitation
is worse than described here, see [`SECURITY.md`](../../SECURITY.md) for
how to report it privately.
