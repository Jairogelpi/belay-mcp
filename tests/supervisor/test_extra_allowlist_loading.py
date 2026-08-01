"""belay/supervisor/server.py::Supervisor._load_extra_allowlist -- R1 fifth
slice (ADR 0024): `belay hooks install --allowlist-extra` writes
`identity.extra_allowlist_pointer_path`; the supervisor best-effort loads
it at construction. `()` (no pointer file at all) is the unchanged,
permissive default. Once that pointer file exists, R1.6 fails closed
instead of open: a broken target returns `ConfigUnavailable` rather than
silently falling back to `()` (no extra entries) -- the whole gate denies
rather than quietly running with less allowlist than the operator
configured.
"""

from __future__ import annotations

from pathlib import Path

from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.server import ConfigUnavailable, Supervisor


def _identity(tmp_path: Path) -> SupervisorIdentity:
    project_anchor = (tmp_path / "belay-hooks.db").resolve()
    return supervisor_identity(project_anchor, belay_home=tmp_path / "home")


def test_no_pointer_file_is_empty(tmp_path: Path) -> None:
    supervisor = Supervisor(_identity(tmp_path))
    assert supervisor._extra_allowlist == ()


def test_pointer_file_to_a_valid_allowlist_file_loads_real_entries(tmp_path: Path) -> None:
    allowlist_file = tmp_path / "extra.txt"
    allowlist_file.write_text("npm run lint\nmake test\n", encoding="utf-8")
    identity = _identity(tmp_path)
    identity.extra_allowlist_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.extra_allowlist_pointer_path.write_text(str(allowlist_file), encoding="utf-8")

    supervisor = Supervisor(identity)
    names = [name for name, _ in supervisor._extra_allowlist]
    assert names == ["npm run lint", "make test"]


def test_pointer_file_to_a_missing_allowlist_file_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.extra_allowlist_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.extra_allowlist_pointer_path.write_text(
        str(tmp_path / "does-not-exist.txt"), encoding="utf-8"
    )

    supervisor = Supervisor(identity)
    assert isinstance(supervisor._extra_allowlist, ConfigUnavailable)


def test_pointer_file_to_an_invalid_allowlist_file_fails_closed(tmp_path: Path) -> None:
    allowlist_file = tmp_path / "bad.txt"
    allowlist_file.write_text("npm run lint; rm -rf /\n", encoding="utf-8")
    identity = _identity(tmp_path)
    identity.extra_allowlist_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.extra_allowlist_pointer_path.write_text(str(allowlist_file), encoding="utf-8")

    supervisor = Supervisor(identity)
    assert isinstance(supervisor._extra_allowlist, ConfigUnavailable)


def test_empty_pointer_file_fails_closed(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    identity.extra_allowlist_pointer_path.parent.mkdir(parents=True, exist_ok=True)
    identity.extra_allowlist_pointer_path.write_text("", encoding="utf-8")

    supervisor = Supervisor(identity)
    assert isinstance(supervisor._extra_allowlist, ConfigUnavailable)
