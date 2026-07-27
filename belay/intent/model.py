"""Intent contract: a per-session execution boundary (adoption/DX, not spec-numbered).

Deliberately narrower than the full pitch ("no refactoring auth", "no new
dependencies" as free English) -- Belay's whole discipline is that nothing
on the safety path is decided by prose an LLM has to interpret (see
`belay/contracts/model.py`, `belay/policy/`). Only what's mechanically
checkable is enforced here:

- `allowed_scope` / `forbidden_scope`: glob patterns against a call's
  `path` argument (the same shape `examples/contracts/fs.yaml` already
  uses) -- a call whose `path` doesn't match `allowed_scope`, or matches
  `forbidden_scope`, is denied before it reaches the upstream.
- `forbidden_tools`: an exact tool-name denylist.
- `budgets.files_changed`: a cap on the number of *distinct* `path`s this
  intent contract has touched so far in the session.

`acceptance` is kept as plain, informational text -- it's what a human (or
`belay export-pr`'s PR body) reads to judge the change, not something this
module claims to verify. Pretending to machine-check "the API wasn't
changed" without a real static-analysis pass would be worse than not
checking it at all.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IntentBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files_changed: int | None = None


class IntentContract(BaseModel):
    """Loaded from YAML (`belay run --intent-contract`); one per session."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    acceptance: list[str] = Field(default_factory=list)
    allowed_scope: list[str] = Field(default_factory=list)
    forbidden_scope: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    budgets: IntentBudgets = Field(default_factory=IntentBudgets)
