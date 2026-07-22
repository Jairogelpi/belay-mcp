# AGENTS.md — rules for the development agent

These rules bind any AI agent (Claude, Codex, or otherwise) working on this
repository. Adapted from `docs/plan.md` §9.

1. **Source of truth is `docs/spec.md` + `docs/plan.md`.** Never invent
   semantics. When something is ambiguous, propose a spec change in a
   separate commit and wait for human approval before building on it.
2. **Strict TDD.** Red test before production code. Never weaken or delete
   a test just to turn CI green.
3. **No `eval`/`exec`/templating** in the expression language. No calling an
   LLM from anywhere under `belay/` — the safety path is deterministic.
4. **English for every public artifact** (code, comments, README, docs,
   CHANGELOG, commit messages). ADRs and working notes may be in Spanish.
5. **Do not rename the repository structure** (`docs/plan.md` §2) without an
   ADR recording why.
6. **One entrega = one PR**, containing: links to the spec sections it
   implements, the list of tests added, and `pytest` (plus the relevant
   conformance level, once E8 exists) output.
7. **Definition of done** for each entrega is its own "(d)" clause in
   `docs/plan.md`; the global definition of done is `docs/plan.md` §0.
