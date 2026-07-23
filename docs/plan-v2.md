# Belay — Plan v2 (post-v0.1.0): E10-E16

Continuation of `docs/plan.md`'s methodology (SDD+TDD, one PR/commit per
entrega, English artifacts, no LLM anywhere in `belay/`, no `eval`/`exec`).
Entregas are additive — they must not weaken any prior test or break L3
conformance (`belay-conformance run --target belay --level 3` must still
pass after each).

## E10 — Statistical anomaly baselines (DONE, shipped)
## E11 — Real SQL dry-run adapter (DONE, shipped)
## E12 — Counterfactual replay (DONE, shipped)
## E13 — Cryptographically signed, offline-verifiable evidence (DONE, shipped)
## E14 — Identity attribution (DONE, shipped)
## E15 — Per-identity irreversible-action quota (DONE, shipped)

See `docs/adr/0010`-`0015` for each.

## E16 — Blast-radius self-explanation returned to the agent (not just the human)

**Problem:** every governance signal Belay has today (caps, anomaly
baseline from E10, quota from E15, policy verdicts) is explained to a
*human* — in `belay approvals list` output, in `belay plan` output, in
`PolicyResult.reasons` that a CLI operator reads. The calling *agent*
itself only ever gets a bare `pending_approval`/`policy_denied` signal
with no legible reason attached to the MCP response it receives. That
means an agent can never self-correct ("oh, this is 40x my normal blast
radius, let me narrow the filter") without a human in the loop first —
every self-correction today requires a pause-and-wait round trip. No
competing tool surfaces risk explanation back to the calling agent inside
the protocol response itself; this is the single most "look, it caught
itself" demoable feature available.

**Constraint:** the explanation must be deterministic, generated from the
same real numbers already computed by `PolicyEngine`/`BaselineStore`
(E10)/`QuotaTracker` (E15)/`Planner` (E4/E11) — NOT a new LLM call, NOT
free-text generation, a template filled with real numbers only. It must
never be the sole gate — a `pause`/`deny` verdict still pauses/denies
exactly as before; this is additive context on top of the existing
verdict, not a new enforcement mechanism, and it must never leak
information that would help a malicious agent evade governance (e.g. do
not reveal the EXACT threshold in a way that trivially invites "ask for
threshold-1" — study this tension explicitly and document the chosen
disclosure policy honestly in the ADR: full transparency is the whole
point of the feature, but check whether spec §12's TOCTOU/security
posture says anything about not over-disclosing policy internals to the
governed agent, and follow it).

**Design:**
- `belay/policy/explain.py`: `explain(policy_result: PolicyResult, plan: Plan) -> Explanation`
  — a pure function building a structured, human-AND-agent-legible
  explanation object from the already-computed `PolicyResult.reasons` (no
  new computation, no re-deriving numbers — this module only formats what
  E4/E10/E15/E11 already produced). `Explanation` has: `verdict`,
  `headline` (one deterministic template sentence per firing dimension,
  e.g. "delete count {observed} is {ratio}x the trailing baseline of
  {baseline} for this session" — reuse the exact numbers already in
  `PolicyResult.reasons`, do not recompute), `dimensions` (list of
  per-dimension structured detail: which of tools/quiet_hours/anomaly/
  quota/irreversible-default fired and why), and `suggested_action` (a
  small deterministic, template-based hint — e.g. "narrow `args.filter`
  and re-plan" for a cap/anomaly-triggered pause on a tool whose contract
  has a `conditions`-bearing filter argument; omit this field entirely,
  do not guess, when no deterministic suggestion applies — never
  fabricate a suggestion that doesn't follow mechanically from the
  contract/policy shape).
- `belay/proxy/lifecycle.py`: attach the `Explanation` to EVERY governed
  response the agent receives, not just `pending_approval` — `allow`
  responses get a minimal/empty-dimensions explanation too (for symmetry
  and so an agent can build a habit of reading it), `pending_approval`
  responses get the full explanation inline (so the agent sees WHY it's
  paused without needing a human to run `belay approvals list` first),
  `policy_denied`/other raised `BelayError`s carry the explanation in
  their error detail payload.
- `belay/proxy/server.py`: ensure the `Explanation` rides in
  `CallToolResult.structuredContent` (extending the existing
  `pending_approval` dict shape, not replacing it) so any standard MCP
  client/agent framework can read it as structured data, not just a
  human-facing string buried in a text block.
- Disclosure policy: document explicitly in the ADR whether exact
  threshold values are included (e.g. "z_score_threshold=3.0",
  "max_irreversible_actions=20") or only relative/comparative language
  ("well above your normal pattern") — pick one, justify it against the
  self-correction goal vs. gaming-the-threshold risk, and apply it
  consistently across all dimensions (don't leak exact numbers for one
  dimension and hide them for another without a stated reason).

**Tests (TDD, red before green):**
- `explain()` given a real `PolicyResult` from an anomaly-triggered pause
  (E10) produces a headline containing the actual observed/baseline
  numbers already in `reasons` — byte-for-byte traceable back to those
  numbers, no invented text.
- Same for a quota-triggered pause (E15): headline cites identity/count/
  window/max from the real `PolicyResult`.
- Same for a cap-triggered pause (E4) and an irreversible-default pause.
- `allow` verdict → `Explanation` with empty/minimal `dimensions`, no
  fabricated concern.
- `suggested_action` is present only when a deterministic rule applies
  (e.g. contract has a `conditions`-bearing narrowing argument) and is
  ABSENT (not a guessed placeholder) otherwise — test both branches
  explicitly.
- End-to-end: a real MCP `call_tool` against a real `belay run` session
  that gets paused by anomaly/quota/cap returns a `CallToolResult` whose
  `structuredContent` contains the full `Explanation`, readable by a
  standard MCP client without any Belay-specific parsing beyond reading
  JSON fields.
- Disclosure-policy test: whatever policy is chosen (exact numbers vs.
  relative language) is applied consistently — write a test enumerating
  all firing dimensions and asserting none of them leaks a raw
  configured-threshold field if the policy says not to (or that all of
  them do, if the policy says full transparency) — this is a real
  regression guard against dimension-by-dimension inconsistency creeping
  in later.
- Property test (Hypothesis): for any `PolicyResult` produced by the real
  `PolicyEngine.evaluate()` across a range of generated inputs, `explain()`
  never raises, never returns a `headline` referencing a number that
  doesn't appear anywhere in `reasons` (traceability property — the
  explanation can never say something the underlying policy evaluation
  didn't actually compute).
- Regression: `pending_approval`/`policy_denied` response *shapes* remain
  backward compatible — existing E3-E15 tests asserting on those response
  dicts must still pass (Explanation is an added field, not a
  replacement of existing keys) — run the full suite and confirm zero
  breaks, do not silently rewrite prior tests' expectations away.

**Exit:** `examples/demo_self_explain.py` shows an agent (simulated,
scripted like the other example scripts — no real LLM) receiving a
`pending_approval` response with a full `Explanation`, printing it,
and — as the demonstrable "wow" moment — the SAME script then
re-planning with a narrower filter *based on reading that explanation's
suggested_action*, resubmitting, and getting `allow` without any human
approval step in between. Run it yourself, confirm this actually happens
end to end, not narrated. Add `docs/adr/0016-e16-blast-radius-self-explanation.md`
documenting: the disclosure policy chosen and why, the traceability
guarantee (every number in the explanation is provably sourced from the
real `PolicyResult`, never invented), and why this is additive context
rather than a new enforcement path (the existing verdict machinery is
completely unchanged; explanation is decoration on top, not a new gate).

Update `CHANGELOG.md`'s `[Unreleased]` section alongside E10-E15's
entries — do not remove or restructure existing entries.

## Sequencing

E16 touches `belay/policy/` (new `explain.py`, reusing but not modifying
`engine.py`'s existing dimension logic), `belay/proxy/lifecycle.py`, and
`belay/proxy/server.py` — safe to build on its own now that E10-E15 are
all landed and pushed. Run the full test suite +
`belay-conformance ... --level 3` after landing.
