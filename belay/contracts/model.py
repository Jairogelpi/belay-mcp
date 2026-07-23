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

import re
from typing import Annotated
from typing import Literal as TLiteral

from pydantic import BaseModel, ConfigDict, Field, model_validator

from belay.contracts.expressions import parse as parse_expression
from belay.errors import BelayError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# plan-v2 E11: the `sql` capture/effect hint (optional, additive -- see
# `SqlHint` below). Deliberately a tiny allow-list, not a real SQL parser:
# contracts are data (spec §4.3's "no user-defined code in contracts"
# principle extended to this statement template), so malformed/unsafe SQL
# must fail loudly at load time, never at execution time (plan-v2 E11 Tests).
_SQL_ALLOWED_VERBS = ("select", "update", "delete")
_SQL_FORBIDDEN_KEYWORDS = (
    "drop",
    "attach",
    "detach",
    "pragma",
    "exec",
    "execute",
    "alter",
    "create",
    "truncate",
    "insert",
    "--",
    "/*",
)


def _validate_sql_statement(statement: str) -> None:
    text = statement.strip()
    if not text:
        raise BelayError("contract_invalid", {"reason": "sql.statement must not be empty"})
    body = text[:-1].strip() if text.endswith(";") else text
    if ";" in body:
        raise BelayError(
            "contract_invalid",
            {"reason": "sql.statement must be a single statement", "statement": statement},
        )
    first_word = re.match(r"[A-Za-z]+", body)
    verb = first_word.group(0).lower() if first_word else ""
    if verb not in _SQL_ALLOWED_VERBS:
        raise BelayError(
            "contract_invalid",
            {
                "reason": f"sql.statement must start with one of {_SQL_ALLOWED_VERBS}",
                "statement": statement,
            },
        )
    lowered = body.lower()
    for kw in _SQL_FORBIDDEN_KEYWORDS:
        pattern = (
            rf"(?<![A-Za-z0-9_]){re.escape(kw)}(?![A-Za-z0-9_])"
            if kw.isalpha()
            else re.escape(kw)
        )
        if re.search(pattern, lowered):
            raise BelayError(
                "contract_invalid",
                {
                    "reason": f"sql.statement contains forbidden keyword {kw!r}",
                    "statement": statement,
                },
            )


class Undo(_Strict):
    tool: str
    args: dict[str, object]


class Capture(_Strict):
    tool: str
    args: dict[str, object]
    as_: Annotated[str, Field(alias="as")]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SqlHint(_Strict):
    """Optional `sql` capture/effect hint (plan-v2 E11) enabling `sql_simulator`.

    `statement` is a bind-parameter SQL template (`:name` placeholders, one
    statement, `SELECT`/`UPDATE`/`DELETE` only); `params` maps each bind name
    to a Belay expression (spec §4.3) evaluated against `$args`/`$context` at
    plan time -- the same expression grammar already used by `undo.args`, so
    there is no second templating language to validate or secure.
    """

    statement: str
    params: dict[str, str] | None = None

    @model_validator(mode="after")
    def _validate_sql(self) -> SqlHint:
        _validate_sql_statement(self.statement)
        for expr in (self.params or {}).values():
            parse_expression(expr)  # raises expression_invalid if malformed
        return self


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
    sql: SqlHint | None = None

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
