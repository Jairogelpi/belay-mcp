from __future__ import annotations

import pytest

from conformance.registry import resolve_target
from conformance.target import ConformanceTarget


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--conformance-target",
        action="store",
        default="belay",
        help="ConformanceTarget to run against: a built-in alias or 'module:ClassName'.",
    )


def pytest_configure(config: pytest.Config) -> None:
    for level in ("l1", "l2", "l3"):
        config.addinivalue_line(
            "markers", f"{level}: conformance level {level[1]} scenario (spec §13)"
        )


@pytest.fixture
def target(request: pytest.FixtureRequest) -> ConformanceTarget:
    return resolve_target(request.config.getoption("--conformance-target"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
