"""PolicyDoc, Cap, Verdict models (spec §6.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal as TLiteral

import yaml
from pydantic import BaseModel, ConfigDict, Field

Verdict = TLiteral["allow", "pause", "deny"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Defaults(_Strict):
    irreversible: Verdict = "pause"
    conditional_unmet: Verdict = "pause"
    unknown_effects: Verdict = "pause"


class CapMatch(_Strict):
    """Selector matched against a plan's effects: `effect` type and/or `resource` glob."""

    effect: str | None = None
    resource: str | None = None


class MaxAmount(_Strict):
    value: float
    currency: str


class Cap(_Strict):
    """A blast-radius cap (spec §6.1). `per` is currently informational only.

    # ponytail: `per: session` needs a session-scoped running total across
    # multiple plans; `PolicyEngine.evaluate()` is a pure function of one
    # plan, so v0.1 evaluates every cap against that single plan only (`per:
    # call` semantics). Add a session accumulator (likely in the ledger, via
    # `plan_created`/`policy_evaluated` replay) when a real multi-call budget
    # is needed.
    """

    match: CapMatch
    max_count: int | None = None
    max_amount: MaxAmount | None = None
    max_recipients: int | None = None
    per: TLiteral["call", "session"] = "call"
    over: Verdict


class ToolRule(_Strict):
    """A per-tool verdict override, matched by glob against the tool name.

    Also the mechanism for relaxing the irreversible default (spec §6.4):
    a tool rule whose verdict is less restrictive than
    `defaults.irreversible` for an irreversible-reversibility plan is a
    relaxation, and must be recorded in the ledger (`config_override`).
    """

    match: str
    verdict: Verdict


class QuietHours(_Strict):
    between: tuple[str, str]
    scope: CapMatch
    verdict: Verdict


class PolicyDoc(_Strict):
    belay_policy: TLiteral["0.1"] = "0.1"
    defaults: Defaults = Field(default_factory=Defaults)
    caps: list[Cap] = Field(default_factory=list)
    tools: list[ToolRule] = Field(default_factory=list)
    quiet_hours: list[QuietHours] = Field(default_factory=list)


def default_policy() -> PolicyDoc:
    """The out-of-the-box policy (spec §6.4): irreversible/unknown effects pause, no caps."""
    return PolicyDoc()


def load_policy(path: str | Path) -> PolicyDoc:
    """Load a policy document (YAML or JSON, spec §6.1) from `path`.

    Operator-facing tooling (the CLI), not a call that crosses the proxy
    boundary toward the agent -- so malformed policy documents raise the
    underlying `yaml.YAMLError`/`pydantic.ValidationError` directly rather
    than one of the 17 codes of spec §11 (none of which covers "policy
    document invalid"; that error model governs what an agent sees, not
    operator config-loading failures).
    """
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    return PolicyDoc.model_validate(raw)


class PolicyResult(_Strict):
    """`PolicyEngine.evaluate()`'s outcome: verdict + the rule ids that fired (spec §6.2)."""

    verdict: Verdict
    reasons: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    relaxations: list[str] = Field(default_factory=list)
