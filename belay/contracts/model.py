"""Pydantic models: Contract, Effect, Undo, Capture, ContractSet (spec §4, Appendix A).

Field validation mirrors the normative JSON Schema of Appendix A, including
its three `allOf` reversibility constraints (§4.2):

- `reversibility: reversible` requires `undo`.
- `reversibility: irreversible` forbids `undo`.
- `reversibility: conditional` requires both `undo` and `conditions`.

Unknown fields anywhere in the document are rejected (spec §14: "the
authority is strict") via `extra="forbid"` on every model.
"""

from __future__ import annotations

from typing import Annotated
from typing import Literal as TLiteral

from pydantic import BaseModel, ConfigDict, Field, model_validator

from belay.contracts.expressions import parse as parse_expression
from belay.errors import BelayError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Undo(_Strict):
    tool: str
    args: dict[str, object]


class Capture(_Strict):
    tool: str
    args: dict[str, object]
    as_: Annotated[str, Field(alias="as")]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Effect(_Strict):
    type: TLiteral["create", "update", "delete", "send", "spend", "execute", "read"]
    resource: str
    count: str | None = None
    amount: dict[str, object] | None = None
    recipients: str | None = None


class Provenance(_Strict):
    declared_by: TLiteral["vendor", "integrator", "community"] | None = None
    verified: bool = False


class Contract(_Strict):
    belay_contract: TLiteral["0.1"]
    tool: Annotated[str, Field(min_length=1)]
    summary: str | None = None
    reversibility: TLiteral["reversible", "irreversible", "conditional"]
    undo: Undo | None = None
    conditions: list[str] | None = None
    capture: Capture | None = None
    idempotent: bool = False
    idempotency_key: str | None = None
    effects: list[Effect]
    verification: dict[str, object] | None = None
    redact: list[str] | None = None
    constraints: dict[str, object] | None = None
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _check_reversibility_constraints(self) -> Contract:
        # spec §4.2 / Appendix A allOf — the three normative constraints.
        if self.reversibility == "reversible" and self.undo is None:
            raise BelayError(
                "contract_invalid",
                {"tool": self.tool, "reason": "reversible contract requires an `undo` block"},
            )
        if self.reversibility == "irreversible" and self.undo is not None:
            raise BelayError(
                "contract_invalid",
                {"tool": self.tool, "reason": "irreversible contract must not declare `undo`"},
            )
        if self.reversibility == "conditional" and (self.undo is None or not self.conditions):
            raise BelayError(
                "contract_invalid",
                {
                    "tool": self.tool,
                    "reason": "conditional contract requires both `undo` and `conditions`",
                },
            )
        if self.conditions:
            for cond in self.conditions:
                parse_expression(cond)  # raises expression_invalid if malformed
        return self


class ContractSet(BaseModel):
    """A resolved, hash-pinned collection of contracts (spec §4.7)."""

    model_config = ConfigDict(extra="forbid")

    contracts: dict[str, Contract]
    set_hash: str

    def resolve(self, tool: str) -> Contract | None:
        return self.contracts.get(tool)
