"""Trivial import test per package (plan.md E0 (c)).

Each belay submodule must import cleanly with no side effects and no
missing dependencies. Real behavior tests arrive with each entrega.
"""

import importlib

import pytest

MODULES = [
    "belay",
    "belay.canonical",
    "belay.errors",
    "belay.contracts",
    "belay.contracts.model",
    "belay.contracts.loader",
    "belay.contracts.expressions",
    "belay.ledger",
    "belay.ledger.model",
    "belay.ledger.store",
    "belay.ledger.verify",
    "belay.ledger.redact",
    "belay.planner",
    "belay.planner.model",
    "belay.planner.planner",
    "belay.policy",
    "belay.policy.model",
    "belay.policy.engine",
    "belay.approvals",
    "belay.approvals.queue",
    "belay.executor",
    "belay.executor.saga",
    "belay.executor.idempotency",
    "belay.executor.recovery",
    "belay.rewind",
    "belay.rewind.service",
    "belay.proxy",
    "belay.proxy.server",
    "belay.proxy.upstream",
    "belay.proxy.lifecycle",
    "belay.cli",
    "belay.cli.main",
    "belay.db",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_error_codes_registry_has_seventeen_entries() -> None:
    from belay.errors import ERROR_CODES

    assert len(ERROR_CODES) == 17
