"""belay/supervisor/auth.py: installation-scoped capability token (ARCH-003/004)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from belay.supervisor.auth import load_or_create_authkey


def test_creates_a_new_random_token_when_none_exists(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "install.key"
    token = load_or_create_authkey(path)
    assert path.is_file()
    assert len(token) == 32
    assert path.read_bytes() == token


def test_loading_twice_returns_the_same_token(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "install.key"
    first = load_or_create_authkey(path)
    second = load_or_create_authkey(path)
    assert first == second


def test_two_different_paths_get_different_tokens(tmp_path: Path) -> None:
    a = load_or_create_authkey(tmp_path / "a.key")
    b = load_or_create_authkey(tmp_path / "b.key")
    assert a != b


def test_concurrent_first_creation_converges_on_one_token(tmp_path: Path) -> None:
    """Two processes racing to create the token file for the very first time
    must never end up with two different tokens (would desync an
    already-connected server/client pair) -- the O_CREAT|O_EXCL exclusive
    create means exactly one thread wins the write; every other thread must
    read back that same winner's bytes, never overwrite them."""
    import threading

    path = tmp_path / "install.key"
    results: list[bytes] = []
    lock = threading.Lock()

    def worker() -> None:
        token = load_or_create_authkey(path)
        with lock:
            results.append(token)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 16
    assert len(set(results)) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions only")
def test_token_file_is_not_world_or_group_readable_on_posix(tmp_path: Path) -> None:
    path = tmp_path / "install.key"
    load_or_create_authkey(path)
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
