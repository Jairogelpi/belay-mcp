"""belay/supervisor/addressing.py: per-installation identity derivation."""

from __future__ import annotations

from pathlib import Path

import pytest
from belay.supervisor.addressing import belay_home, supervisor_identity


def test_same_db_path_yields_the_same_identity(tmp_path: Path) -> None:
    db = tmp_path / "belay-hooks.db"
    a = supervisor_identity(db, belay_home=tmp_path / "home")
    b = supervisor_identity(db, belay_home=tmp_path / "home")
    assert a.install_id == b.install_id
    assert a.address == b.address
    assert a.authkey_path == b.authkey_path


def test_different_db_paths_yield_different_identities(tmp_path: Path) -> None:
    a = supervisor_identity(tmp_path / "a.db", belay_home=tmp_path / "home")
    b = supervisor_identity(tmp_path / "b.db", belay_home=tmp_path / "home")
    assert a.install_id != b.install_id
    assert a.address != b.address


def test_authkey_and_lock_paths_are_outside_the_project_db_directory(tmp_path: Path) -> None:
    db = tmp_path / "project" / "belay-hooks.db"
    home = tmp_path / "home"
    identity = supervisor_identity(db, belay_home=home)
    assert home in identity.authkey_path.parents
    assert home in identity.lock_path.parents
    assert (tmp_path / "project") not in identity.authkey_path.parents


def test_data_path_is_also_outside_the_project_directory(tmp_path: Path) -> None:
    """The P0 fix itself: the authoritative approvals/idempotency database
    must never live inside the project the supervisor is gating."""
    db = tmp_path / "project" / "belay-hooks.db"
    home = tmp_path / "home"
    identity = supervisor_identity(db, belay_home=home)
    assert home in identity.data_path.parents
    assert (tmp_path / "project") not in identity.data_path.parents
    assert identity.data_path != db


def test_data_path_is_stable_and_unique_per_project(tmp_path: Path) -> None:
    home = tmp_path / "home"
    a = supervisor_identity(tmp_path / "a.db", belay_home=home)
    b = supervisor_identity(tmp_path / "a.db", belay_home=home)
    c = supervisor_identity(tmp_path / "b.db", belay_home=home)
    assert a.data_path == b.data_path
    assert a.data_path != c.data_path


def test_belay_home_respects_belay_home_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-belay-home"
    monkeypatch.setenv("BELAY_HOME", str(override))
    assert belay_home() == override


def test_supervisor_identity_without_explicit_belay_home_uses_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "custom-belay-home"
    monkeypatch.setenv("BELAY_HOME", str(override))
    identity = supervisor_identity(tmp_path / "belay-hooks.db")
    assert override in identity.data_path.parents
    assert override in identity.authkey_path.parents
