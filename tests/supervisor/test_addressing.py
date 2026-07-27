"""belay/supervisor/addressing.py: per-installation identity derivation."""

from __future__ import annotations

from pathlib import Path

from belay.supervisor.addressing import supervisor_identity


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
