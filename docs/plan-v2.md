# Belay — Plan v2 (post-v0.1.0): E10-E13

Continuation of `docs/plan.md`'s methodology (SDD+TDD, one PR/commit per
entrega, English artifacts, no LLM anywhere in `belay/`, no `eval`/`exec`).
Entregas are additive — they must not weaken any prior test or break L3
conformance (`belay-conformance run --target belay --level 3` must still
pass after each).

## E10 — Statistical anomaly baselines (DONE, shipped)

Landed. See `docs/adr/0010-e10-anomaly-baselines.md`.

## E11 — Real SQL dry-run adapter (DONE, shipped)

Landed. See `docs/adr/0011-e11-sql-dry-run.md`.

## E12 — Counterfactual replay (DONE, shipped)

Landed. See `docs/adr/0012-e12-counterfactual-replay.md`.

## E13 — Cryptographically signed, offline-verifiable evidence

**Problem:** the ledger's hash chain (E2, spec §9.2) proves internal
consistency — that no event was altered or reordered — but only to someone
who trusts the party presenting the chain and has access to recompute it.
There is no artifact today that a *third party*, with no access to Belay's
database and no trust relationship with the operator, can verify
independently: "this exact sequence of governed actions and their
compensations really happened, was really approved by this identified
human, and has not been altered since." That's the difference between an
internal audit log and portable, legally-useful evidence. Plan.md itself
flags signed contract sets as a v0.2+ direction (§11, "firma de contract
sets vía sigstore") — this entrega generalizes the idea to the whole
session ledger, using plain Ed25519 (no sigstore/external CA dependency
required for v1, keep it self-contained and offline-first).

**Constraint:** fully offline-verifiable — verification must need nothing
but the exported evidence file and a public key; no call to Belay, no
database access, no network. No LLM, no `eval`/`exec` (same as always).
Must not change the existing hash-chain algorithm or break any E2 test —
this is an additive signature *over* the existing chain, not a replacement
of it.

**Design:**
- `belay/ledger/signing.py`:
  - `SigningKey` wrapper around Ed25519 (use `cryptography` — a
    well-audited, already-common Python dependency; do not hand-roll
    crypto primitives). `generate() -> SigningKey`, `.public_bytes()`,
    key persisted to disk as a file the operator controls (never stored
    inside the SQLite ledger DB itself — signing key and evidence must be
    separable, that's the whole point).
  - `sign_session(events: list[Event], key: SigningKey) -> SignedEvidence`
    — computes the existing chain's terminal hash (reuse
    `belay/ledger/verify.py`'s `verify_chain` logic to get the final
    `hash`, do not recompute the chain a second way), then signs
    `canonical_bytes({"session_id", "set_hash", "chain_head_hash",
    "event_count", "signed_at"})` (reuse `belay/canonical.py`, do not
    invent a second canonicalization) with the private key.
  - `SignedEvidence` model: the signature, the public key (or its
    fingerprint), the signed summary fields, and — critically — the full
    event list itself (or a reference/embedded export) so the bundle is
    self-contained and doesn't require the verifier to already have the
    events from elsewhere.
  - `verify_evidence(bundle: SignedEvidence) -> VerificationResult` — pure
    function, no I/O beyond reading the bundle passed in: (1) recompute
    the hash chain over the embedded events via the *existing*
    `verify_chain`/`verify_coherence` from E2 (reuse, don't duplicate),
    (2) recompute the canonical summary and check the Ed25519 signature
    against the embedded/provided public key, (3) report tampering
    precisely — which check failed (chain broke at event k vs. signature
    invalid vs. summary mismatch) rather than a single opaque
    pass/fail, matching the existing precision of E2's chain-corruption
    reporting (`verify_chain` already reports the exact failing index —
    this must not regress that precision when composed with signing).
- Export format: a single self-contained JSON (or JSON+detached
  signature, your call — document the choice) file, `belay verify-export
  <session_id> --key <path> -o <file>`, and a fully independent verifier
  entry point that needs ONLY that file (+ the public key, embedded or
  supplied separately depending on your trust-model choice — document
  it) — `belay verify-evidence <file> [--pubkey <path>]`. This verifier
  path must be usable **without a live Belay installation's database at
  all** — test this by verifying an exported bundle in a fresh temp dir
  with no `belay.db` present.
- Tamper detection must cover: (a) any event payload byte changed post-export
  → chain check fails at the right index, (b) the whole file re-signed with
  a different key → signature check fails, (c) the summary fields
  (session_id/set_hash/event_count) edited without re-signing → signature
  check fails, (d) events appended after signing → detected (event_count
  or chain-head mismatch against the signed summary).

**Tests (TDD, red before green):**
- Sign a real multi-event session (reuse an E3/E6/E7-style real
  stdio-subprocess fixture) → `verify_evidence` reports fully valid.
- Each of the four tamper scenarios above, each as its own test, each
  asserting the *specific* failure reported (not just "invalid").
- Property test (Hypothesis): for any valid signed evidence bundle,
  flipping any single byte in the embedded event payloads always fails
  verification (never a false negative) — the security-critical guarantee
  for this entrega, do not skip it.
- Wrong public key supplied to `verify_evidence` → signature check fails
  cleanly, no crash.
- Round-trip: `belay verify-export` then `belay verify-evidence` against
  the exported file in a directory with no `belay.db` at all, no live
  `belay run`, confirming the "no Belay installation needed" claim for
  real, not just architecturally.
- Signing key never appears inside the SQLite ledger DB or the exported
  evidence file itself (only the public key/fingerprint does) — an
  explicit test grepping the exported bytes for the private key material
  to prove it never leaks in.
- Regression: existing E2 `verify_chain`/`verify_coherence` tests and CLI
  `belay verify` behavior are completely unaffected — signing is additive,
  never required, `belay verify` (unsigned path) keeps working exactly as
  before for anyone who doesn't opt into signing.

**Exit:** `examples/demo_signed_evidence.py` (or an added step to an
existing demo script) runs a real session, exports signed evidence, then
verifies it in a clean subdirectory with no access to the original
`belay.db`, and additionally demonstrates a tamper attempt (flip one byte
in a copy of the exported file) being caught with a precise error. Add
`docs/adr/0013-e13-signed-evidence.md` documenting: why Ed25519 over
sigstore/X.509 for v1 (self-contained, no CA/network dependency, upgrade
path noted for later), the exact tamper-detection guarantees and their
limits (e.g. this proves *the operator's key* signed it, not identity of
the human approver beyond what's already recorded in `approved_by` fields
— be honest about what cryptographic non-repudiation does and doesn't
give you here), and the key-management responsibility handed to the
operator (Belay generates/uses keys but does not manage key rotation or
revocation in v1 — note as a documented gap/future issue, don't
overclaim).

Update `CHANGELOG.md`'s `[Unreleased]` section alongside the existing
E10/E11/E12 entries — do not remove or restructure those.

## Sequencing

E13 builds on `belay/ledger/verify.py` and `belay/canonical.py` (E2) only
by reuse, not modification — safe to build independently of E10/E11/E12
which are all already landed. Run the full test suite +
`belay-conformance ... --level 3` after landing to catch cross-contamination
early.
