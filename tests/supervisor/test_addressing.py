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


def test_posix_socket_address_stays_short_even_under_a_long_belay_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (plan-v2 E19.7, found via real macOS CI: `OSError: AF_UNIX
    path too long`): the AF_UNIX socket address must stay well under the
    ~104-108 byte sun_path limit even when belay_home itself is long (a
    real risk on macOS in particular, whose own $TMPDIR/test tmp_path
    layout is notoriously deep) -- by living under the system temp
    directory (always kept short by the OS for exactly this reason)
    instead of under belay_home. Exercised on Windows too via monkeypatch
    -- sys.platform is checked dynamically inside supervisor_identity(),
    not cached at import time, so this is a legitimate way to exercise the
    POSIX branch's own logic from any host."""
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("USER", "test-user")
    long_home = tmp_path / ("a" * 40) / ("b" * 40) / ("c" * 40) / "home"

    identity = supervisor_identity(tmp_path / "belay-hooks.db", belay_home=long_home)

    assert len(identity.address.encode("utf-8")) < 100
    assert str(long_home) not in identity.address
    # lock/data/authkey are ordinary file opens -- no such length limit,
    # unaffected by this change, still under the (long) belay_home.
    assert long_home in identity.lock_path.parents
    assert long_home in identity.data_path.parents


def test_posix_socket_address_is_namespaced_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shared /tmp on a multi-user machine must not let two different OS
    users' installs collide on the same socket path -- home-directory-based
    paths got this for free via $HOME, the system-tempdir-based path needs
    it explicitly."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("USER", "alice")
    alice = supervisor_identity(Path("/some/project/belay-hooks.db"))
    monkeypatch.setenv("USER", "bob")
    bob = supervisor_identity(Path("/some/project/belay-hooks.db"))
    assert alice.address != bob.address
    assert "alice" in alice.address
    assert "bob" in bob.address
