"""belay/supervisor/lifecycle.py: spawn-lock mechanics (concurrent spawn
coordination, stale-lock reclaim). The successful spawn+connect path itself
is covered end-to-end by tests/cli/test_hooks_lifecycle.py's `hooks_run`
tests, which spawn a real detached supervisor process.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import belay.supervisor.lifecycle as lifecycle_module
import pytest
from belay.supervisor.addressing import SupervisorIdentity, supervisor_identity
from belay.supervisor.lifecycle import _acquire_spawn_lock, _release_spawn_lock


def _identity(tmp_path: Path) -> SupervisorIdentity:
    return supervisor_identity(tmp_path / "belay-hooks.db", belay_home=tmp_path / "home")


def test_first_caller_acquires_the_lock(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    assert _acquire_spawn_lock(identity) is True
    assert identity.lock_path.is_file()


def test_second_caller_does_not_acquire_a_fresh_lock(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    assert _acquire_spawn_lock(identity) is True
    assert _acquire_spawn_lock(identity) is False


def test_release_then_reacquire_succeeds(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    assert _acquire_spawn_lock(identity) is True
    _release_spawn_lock(identity)
    assert _acquire_spawn_lock(identity) is True


def test_stale_lock_is_reclaimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_module, "_LOCK_STALE_AFTER_S", 0.05)
    identity = _identity(tmp_path)
    assert _acquire_spawn_lock(identity) is True  # first process "crashes" without releasing
    time.sleep(0.1)
    assert _acquire_spawn_lock(identity) is True  # reclaimed as stale, not stuck forever


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_lock_file_is_not_world_readable_on_posix(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    _acquire_spawn_lock(identity)
    mode = identity.lock_path.stat().st_mode & 0o777
    assert mode == 0o600
