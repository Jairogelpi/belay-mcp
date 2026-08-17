# Security Policy

See [`docs/security/threat-model.md`](docs/security/threat-model.md) for
what Belay's Native Agent Gate and MCP proxy actually protect against, and
what they explicitly do not — read that before deciding whether something
you found is a vulnerability or a stated limitation.

## Scope

Belay's safety path — `belay/contracts`, `belay/planner`, `belay/policy`,
`belay/approvals`, `belay/executor`, `belay/rewind`, `belay/ledger`,
`belay/intent`, `belay/supervisor`, `belay/hooks` — is deterministic: no
`eval`/`exec`, no LLM calls, no network calls beyond MCP to configured tool
servers. This is the code that matters most from a security standpoint,
since it's what stands between an agent and an irreversible action.

`belay/cli` (adoption/DX: install, doctor, dashboard, export-pr) is lower
stakes but still in scope if a bug there could corrupt the ledger, forge
evidence, or weaken a policy verdict.

## What counts as a security issue here

- Bypassing a `deny`/`pause` policy verdict without a genuine approval.
- Forging, corrupting, or silently reordering a ledger event (breaking the
  hash chain, spec §9) without detection by `belay verify`.
- Getting the contract expression language (spec §4.3) to execute arbitrary
  code — it's a closed grammar, never `eval`/`exec`, by design.
- Self-approval: any code path that lets an agent approve its own action
  (spec §12 explicitly forbids this).
- Reading or exfiltrating Belay's own private storage (capability token,
  approvals database under `belay_home()`) through a call the Native Agent
  Gate (`belay/hooks/gate.py`) was supposed to have classified as safe.
- Forging or replaying a signed evidence bundle (Ed25519, E13) so it
  verifies against a different action than the one it was actually issued
  for.

## What is explicitly out of scope / already a known limitation

Belay's threat model does **not** claim to resist an arbitrary process
running as the same OS user as the agent (see
[`docs/security/threat-model.md`](docs/security/threat-model.md) and the
"TRUTH-010" invariant referenced in `belay/hooks/gate.py` and
`belay/hooks/claude_code_adapter.py`) — that's a stated limitation, not a
vulnerability report. If you find a gap in that statement itself (e.g. a
claim of same-user protection that the code doesn't actually deliver),
that *is* worth reporting.

## Reporting a vulnerability

Please **do not** open a public issue with exploit details. Instead, use
GitHub's private reporting flow:
[Report a vulnerability](https://github.com/Jairogelpi/belay-mcp/security/advisories/new)
(repo → Security tab → "Report a vulnerability").

As of this writing, GitHub's private vulnerability reporting feature has
not yet been enabled on this repository — it's part of
[`docs/release-runbook.md`](docs/release-runbook.md)'s GitHub settings
rollout (E23 Task 7), applied only after a verified `v0.2.0a1`
prerelease exists, not before. Until it's confirmed enabled (the runbook
includes the exact readback command to check), the link above may 404 or
show as unavailable.

If that's unavailable, open an issue asking for a private channel rather
than describing the exploit inline — see
[`CONTRIBUTING.md`](CONTRIBUTING.md)'s Security section.

Include, where possible: the specific tool/pack/contract involved, the
policy or contract configuration in use, and a minimal repro (a failing
test is ideal, per this project's TDD convention).

## Supported versions

Belay is pre-1.0 (`0.2.0a1`, unreleased to PyPI/npm as of this writing —
see "Release status" in [`README.md`](README.md)). Until a 1.0 is tagged,
only `main` is supported; there is no parallel maintenance of older minor
versions.
