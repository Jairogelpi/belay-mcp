"""belay/supervisor/server.py::Supervisor._load_contract_set -- R1 first
slice: `belay hooks install --contracts <file>` writes
`identity.contracts_pointer_path`; the supervisor best-effort loads it at
construction time. `None` (no pointer file at all) is the unchanged,
permissive default. Once that pointer file exists, though, R1.6 fails
closed instead of open: a broken target returns `ConfigUnavailable`
rather than silently falling back to `None`, since silently reverting to
unconfigured behavior is exactly the gap R1.6 closes.
"""

from __future__ import annotations

from pathlib import Path

from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.server import ConfigUnavailable, Supervisor


def _identity(tmp_path: Path) -> SupervisorIdentity:
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    return supervisor_identity(project_anchor, belay_home=tmp_path / "home")


def test_no_pointer_file_is_none(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    supervisor = Supervisor(identity)
    assert supervisor._contract_set is None


def test_pointer_file_to_a_valid_contracts_file_loads_a_real_contract_set(
    tmp_path: Path,
) -> None:
    contracts_file = tmp_path / "contracts.yaml"
    contracts_file.write_text(
        "belay_contract: '0.1'\n"
        "tool: Write\n"
        "reversibility: irreversible\n"
        "effects:\n"
        "  - type: update\n"
        "    resource: native.file\n",
        encoding="utf-8",
    )
    identity = _identity(tmp_path)
    identity.contracts_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.contracts_pointer_path.write_text(str(contracts_file), encoding="utf-8")

    supervisor = Supervisor(identity)
    assert supervisor._contract_set is not None
    assert supervisor._contract_set.resolve("Write") is not None
    assert supervisor._contract_set.resolve("Edit") is None


def test_pointer_file_to_a_missing_contracts_file_fails_closed(
    tmp_path: Path,
) -> None:
    """R1.6: a pointer file that exists (this install opted in) but
    points at a now-missing target must fail closed, not silently revert
    to `None` (unconfigured/permissive)."""
    identity = _identity(tmp_path)
    identity.contracts_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.contracts_pointer_path.write_text(
        str(tmp_path / "does-not-exist.yaml"), encoding="utf-8"
    )

    supervisor = Supervisor(identity)
    assert isinstance(supervisor._contract_set, ConfigUnavailable)


def test_pointer_file_to_invalid_contract_content_fails_closed(
    tmp_path: Path,
) -> None:
    contracts_file = tmp_path / "broken.yaml"
    contracts_file.write_text("belay_contract: '0.1'\ntool: Write\n", encoding="utf-8")
    identity = _identity(tmp_path)
    identity.contracts_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.contracts_pointer_path.write_text(str(contracts_file), encoding="utf-8")

    supervisor = Supervisor(identity)
    assert isinstance(supervisor._contract_set, ConfigUnavailable)


def test_empty_pointer_file_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.contracts_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.contracts_pointer_path.write_text("", encoding="utf-8")

    supervisor = Supervisor(identity)
    assert isinstance(supervisor._contract_set, ConfigUnavailable)
