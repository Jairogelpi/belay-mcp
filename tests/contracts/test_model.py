"""Contract validation per spec §4.1-4.2 and Appendix A's `allOf` constraints."""

from __future__ import annotations

import pytest
from belay.contracts.model import Contract
from belay.errors import BelayError
from pydantic import ValidationError

BASE = {
    "belay_contract": "0.1",
    "tool": "crm.create_record",
    "reversibility": "reversible",
    "undo": {"tool": "crm.delete_record", "args": {"id": "$result.id"}},
    "effects": [{"type": "create", "resource": "crm.record", "count": "1"}],
}


def test_reversible_without_undo_is_contract_invalid() -> None:
    doc = {**BASE, "reversibility": "reversible"}
    del doc["undo"]
    with pytest.raises(BelayError) as exc_info:
        Contract.model_validate(doc)
    assert exc_info.value.code == "contract_invalid"


def test_irreversible_with_undo_is_invalid() -> None:
    doc = {**BASE, "reversibility": "irreversible"}
    with pytest.raises(BelayError) as exc_info:
        Contract.model_validate(doc)
    assert exc_info.value.code == "contract_invalid"


def test_irreversible_without_undo_is_valid() -> None:
    doc = {**BASE, "reversibility": "irreversible"}
    del doc["undo"]
    contract = Contract.model_validate(doc)
    assert contract.reversibility == "irreversible"


def test_conditional_requires_undo_and_conditions() -> None:
    doc = {**BASE, "reversibility": "conditional"}
    with pytest.raises(BelayError) as exc_info:
        Contract.model_validate(doc)
    assert exc_info.value.code == "contract_invalid"


def test_conditional_with_undo_but_no_conditions_is_invalid() -> None:
    doc = {**BASE, "reversibility": "conditional", "undo": BASE["undo"]}
    with pytest.raises(BelayError) as exc_info:
        Contract.model_validate(doc)
    assert exc_info.value.code == "contract_invalid"


def test_conditional_with_undo_and_conditions_is_valid() -> None:
    doc = {
        **BASE,
        "reversibility": "conditional",
        "conditions": ["$result.id != null"],
    }
    contract = Contract.model_validate(doc)
    assert contract.conditions == ["$result.id != null"]


def test_conditional_condition_expressions_must_be_in_grammar() -> None:
    doc = {
        **BASE,
        "reversibility": "conditional",
        "conditions": ["__import__('os')"],
    }
    with pytest.raises(BelayError) as exc_info:
        Contract.model_validate(doc)
    assert exc_info.value.code == "expression_invalid"


def test_reversible_with_undo_is_valid() -> None:
    contract = Contract.model_validate(BASE)
    assert contract.reversibility == "reversible"
    assert contract.undo is not None
    assert contract.undo.tool == "crm.delete_record"


def test_unknown_top_level_field_is_rejected() -> None:
    doc = {**BASE, "totally_unknown_field": True}
    with pytest.raises(ValidationError):
        Contract.model_validate(doc)


def test_unknown_nested_undo_field_is_rejected() -> None:
    doc = {**BASE, "undo": {"tool": "x", "args": {}, "surprise": 1}}
    with pytest.raises(ValidationError):
        Contract.model_validate(doc)


def test_capture_block_requires_tool_args_as() -> None:
    doc = {
        **BASE,
        "capture": {"tool": "crm.get_record", "args": {"id": "$args.id"}, "as": "before"},
    }
    contract = Contract.model_validate(doc)
    assert contract.capture is not None
    assert contract.capture.as_ == "before"


def test_effects_type_is_restricted_to_the_seven_declared_types() -> None:
    doc = {
        **BASE,
        "effects": [{"type": "not_a_real_type", "resource": "x"}],
    }
    with pytest.raises(ValidationError):
        Contract.model_validate(doc)
