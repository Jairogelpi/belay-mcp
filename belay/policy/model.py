"""PolicyDoc, Cap, Verdict models (spec §6.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal as TLiteral

import yaml
from pydantic import BaseModel, ConfigDict, Field

Verdict = TLiteral["allow", "pause", "deny"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnomalyDefaults(_Strict):
    """Statistical anomaly baseline config (plan-v2 E10) -- zero manual thresholds required.

    Defaults are chosen so the dimension works out of the box with no
    operator configuration: `min_samples` calls build the baseline (cold
    start, never blocks below it), then any call whose effect count is
    `z_score_threshold` standard deviations above the trailing mean pauses.
    `exclude` is the per-tool opt-out (globs), the anomaly-dimension
    equivalent of relaxing the irreversible default (spec §6.4).
    """

    enabled: bool = True
    min_samples: int = 10
    z_score_threshold: float = 3.0
    verdict: Verdict = "pause"
    exclude: list[str] = Field(default_factory=list)


class QuotaDefaults(_Strict):
    """Per-identity rolling irreversible-action quota config (plan-v2 E15).

    Unlike `AnomalyDefaults`, this is NOT a zero-config statistically-derived
    number -- `max_irreversible_actions` is an operator judgment call (see
    docs/adr/0015-e15-identity-quota.md for the honest caveat). `enabled`
    defaults to `False`: no default limit is imposed on an identity's
    irreversible-action volume until an operator explicitly turns it on and
    picks a number appropriate to their org, unlike E10's anomaly detection
    which is safe to enable unconditionally.
    """

    enabled: bool = False
    window: str = "1d"
    max_irreversible_actions: int = 20
    verdict: Verdict = "pause"


class Defaults(_Strict):
    irreversible: Verdict = "pause"
    conditional_unmet: Verdict = "pause"
    unknown_effects: Verdict = "pause"
    anomaly: AnomalyDefaults = Field(default_factory=AnomalyDefaults)
    quota: QuotaDefaults = Field(default_factory=QuotaDefaults)


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
