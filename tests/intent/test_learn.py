"""belay/intent/learn.py: compiling a rejection into a durable rule."""

from __future__ import annotations

import pytest
from belay.approvals.queue import ApprovalQueue
from belay.intent.learn import apply_rule, propose_rule


def _rejected_item(reason: str, path: str | None = None):
    queue = ApprovalQueue()
    args = {"path": path} if path else {}
    item = queue.request("s_test", "p_1", {"tool": "crm.bulk_delete", "args": args})
    return queue.reject(item.approval_id, rejected_by="jairo", reason=reason)


def test_propose_rule_on_pending_item_raises() -> None:
    queue = ApprovalQueue()
    pending = queue.request("s_test", "p_1", {"tool": "crm.bulk_delete", "args": {}})
    with pytest.raises(ValueError, match="not 'rejected'"):
        propose_rule(pending)


def test_propose_rule_suggests_forbidden_tool() -> None:
    item = _rejected_item("too risky")
    rules = propose_rule(item)
    tool_rules = [r for r in rules if r.kind == "forbidden_tools"]
    assert len(tool_rules) == 1
    assert tool_rules[0].value == "crm.bulk_delete"
    assert tool_rules[0].reason == "too risky"


def test_propose_rule_suggests_forbidden_scope_when_path_present() -> None:
    item = _rejected_item("bad path", path="src/auth/login.py")
    rules = propose_rule(item)
    scope_rules = [r for r in rules if r.kind == "forbidden_scope"]
    assert len(scope_rules) == 1
    assert scope_rules[0].value == "src/auth/**"


def test_apply_rule_creates_fresh_contract_with_tool_forbidden(tmp_path) -> None:
    item = _rejected_item("too risky")
    rule = propose_rule(item)[0]
    contract_path = tmp_path / "learned.yaml"
    contract = apply_rule(str(contract_path), rule)
    assert "crm.bulk_delete" in contract.forbidden_tools
    assert contract_path.is_file()


def test_apply_rule_does_not_duplicate_existing_entry(tmp_path) -> None:
    item = _rejected_item("too risky")
    rule = propose_rule(item)[0]
    contract_path = tmp_path / "learned.yaml"
    apply_rule(str(contract_path), rule)
    contract = apply_rule(str(contract_path), rule)
    assert contract.forbidden_tools.count("crm.bulk_delete") == 1
