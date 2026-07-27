"""On-demand supervisor lifecycle (spec ARCH-008): connect to an
already-running supervisor when there is one; otherwise spawn one as a
detached background process and wait for it to come up. No system-service
integration yet (ARCH-008's service-manager integration is P1) -- on-demand
start is the required P0 fallback and the only mechanism implemented so
far, said plainly.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from multiprocessing.connection import Client

from belay.supervisor.addressing import SupervisorIdentity
from belay.supervisor.auth import load_or_create_authkey

_SPAWN_WAIT_TIMEOUT_S = 5.0
_LOCK_STALE_AFTER_S = 15.0


def is_listening(identity: SupervisorIdentity) -> bool:
    authkey = load_or_create_authkey(identity.authkey_path)
    try:
        conn = Client(identity.address, authkey=authkey)
    except OSError:
        return False
    conn.close()
    return True


def _acquire_spawn_lock(identity: SupervisorIdentity) -> bool:
    """True if this process won the right to spawn (or reclaimed a stale
    lock); False if another process is already spawning one and this one
    should just wait/poll instead of racing it."""
    identity.lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(identity.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - identity.lock_path.stat().st_mtime
        except OSError:
            return False
        if age > _LOCK_STALE_AFTER_S:
            identity.lock_path.unlink(missing_ok=True)
            return _acquire_spawn_lock(identity)
        return False


def _release_spawn_lock(identity: SupervisorIdentity) -> None:
    identity.lock_path.unlink(missing_ok=True)


def _spawn_detached(db_path: str) -> None:
    args = [sys.executable, "-m", "belay.cli.main", "supervisor", "serve", "--db", db_path]
    if sys.platform == "win32":
        detached_process = 0x00000008
        create_new_process_group = 0x00000200
        subprocess.Popen(
            args,
            creationflags=detached_process | create_new_process_group,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            args,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )


def _wait_until_listening(identity: SupervisorIdentity, deadline: float) -> bool:
    while time.monotonic() < deadline:
        if is_listening(identity):
            return True
        time.sleep(0.1)
    return is_listening(identity)


def ensure_running(identity: SupervisorIdentity, db_path: str) -> bool:
    """Returns True once a supervisor is reachable, False if it could not be
    reached within the spawn+wait budget -- the caller must then fail
    closed (spec 8.1), never block indefinitely on a mutation decision."""
    if is_listening(identity):
        return True

    if _acquire_spawn_lock(identity):
        try:
            _spawn_detached(db_path)
            return _wait_until_listening(identity, time.monotonic() + _SPAWN_WAIT_TIMEOUT_S)
        finally:
            _release_spawn_lock(identity)

    # Another process is already spawning one -- wait for it instead of
    # racing to start a second supervisor for the same install.
    return _wait_until_listening(identity, time.monotonic() + _SPAWN_WAIT_TIMEOUT_S)
