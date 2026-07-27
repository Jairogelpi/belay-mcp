"""belay/intent/enforce.py: the pre-execution intent-contract gate.

Covers the two real bugs a review caught before this landed (2026-07-27):
path-traversal bypass via unnormalized `fnmatch`, and a no-`path` tool call
silently exempted from an active scope.
"""

from __future__ import annotations

from belay.intent.enforce import check_intent_contract
from belay.intent.model import IntentBudgets, IntentContract


def _contract(**kwargs: object) -> IntentContract:
    return IntentContract(intent="test", **kwargs)  # type: ignore[arg-type]


def test_allows_call_with_no_contract_restrictions() -> None:
    contract = _contract()
    assert check_intent_contract(contract, "fs.write_file", {"path": "a.py"}, frozenset()) is None


def test_forbidden_tool_denied_regardless_of_path() -> None:
    contract = _contract(forbidden_tools=["crm.bulk_delete"])
    violation = check_intent_contract(contract, "crm.bulk_delete", {}, frozenset())
    assert violation is not None
    assert violation.reason == "forbidden_tool"


def test_in_scope_write_allowed() -> None:
    contract = _contract(allowed_scope=["src/profile/**"])
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "src/profile/tz.py"}, frozenset()
    )
    assert violation is None


def test_out_of_scope_write_denied() -> None:
    contract = _contract(allowed_scope=["src/profile/**"])
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "random/other.py"}, frozenset()
    )
    assert violation is not None
    assert violation.reason == "out_of_scope"


def test_forbidden_scope_denied_even_if_in_allowed_scope() -> None:
    contract = _contract(allowed_scope=["src/**"], forbidden_scope=["src/auth/**"])
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "src/auth/login.py"}, frozenset()
    )
    assert violation is not None
    assert violation.reason == "forbidden_scope"


def test_path_traversal_denied_before_glob_matching() -> None:
    """`src/profile/../../../etc/passwd` must never be matched against `src/**`
    as a raw string -- normalize first, then refuse anything that still
    escapes upward. This was a real bypass found in review."""
    contract = _contract(allowed_scope=["src/profile/**"])
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "src/profile/../../../etc/passwd"}, frozenset()
    )
    assert violation is not None
    assert violation.reason == "path_escapes_scope"


def test_absolute_path_denied() -> None:
    contract = _contract(allowed_scope=["src/**"])
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "/etc/passwd"}, frozenset()
    )
    assert violation is not None
    assert violation.reason == "path_escapes_scope"


def test_windows_drive_path_denied() -> None:
    contract = _contract(allowed_scope=["src/**"])
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "C:/Windows/System32/config"}, frozenset()
    )
    assert violation is not None
    assert violation.reason == "path_escapes_scope"


def test_backslash_traversal_denied() -> None:
    contract = _contract(allowed_scope=["src/**"])
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "src\\..\\..\\secrets.txt"}, frozenset()
    )
    assert violation is not None
    assert violation.reason == "path_escapes_scope"


def test_no_path_arg_denied_when_scope_is_active() -> None:
    """A tool call with no `path` argument must not silently bypass an active
    scope -- found in review as a real gap (e.g. a shell tool with no path
    arg at all)."""
    contract = _contract(allowed_scope=["src/**"])
    violation = check_intent_contract(contract, "shell.exec", {"cmd": "rm -rf /"}, frozenset())
    assert violation is not None
    assert violation.reason == "unscoped_call"


def test_no_path_arg_allowed_when_no_scope_declared() -> None:
    contract = _contract(forbidden_tools=["other.tool"])
    violation = check_intent_contract(contract, "shell.exec", {"cmd": "ls"}, frozenset())
    assert violation is None


def test_files_changed_budget_enforced() -> None:
    contract = _contract(budgets=IntentBudgets(files_changed=1))
    ok = check_intent_contract(contract, "fs.write_file", {"path": "a.py"}, frozenset())
    assert ok is None
    exceeded = check_intent_contract(
        contract, "fs.write_file", {"path": "b.py"}, frozenset({"a.py"})
    )
    assert exceeded is not None
    assert exceeded.reason == "budget_exceeded"


def test_files_changed_budget_allows_repeat_write_to_same_file() -> None:
    contract = _contract(budgets=IntentBudgets(files_changed=1))
    violation = check_intent_contract(
        contract, "fs.write_file", {"path": "a.py"}, frozenset({"a.py"})
    )
    assert violation is None
