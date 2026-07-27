"""Per-installation supervisor identity: where it listens, and where its
capability token lives -- both derived deterministically from the
`ApprovalQueue` database path a `belay hooks install` was pointed at, so
each project/install gets its own independent supervisor process and
capability rather than one global daemon shared across unrelated projects
(spec ARCH-003: "installation-scoped capability").
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _belay_home() -> Path:
    """User-scoped, outside any project directory -- an agent restricted to
    project-directory tool calls cannot read or tamper with anything stored
    here (spec ARCH-003/004: not a project file, not solely an env var)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "belay"
    return Path.home() / ".belay"


def _install_id(db_path: Path) -> str:
    return hashlib.sha256(str(db_path.resolve()).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SupervisorIdentity:
    install_id: str
    #: `multiprocessing.connection` address: a Windows named-pipe path
    #: (`\\.\pipe\...`) on win32, a Unix domain socket path elsewhere --
    #: never an unauthenticated TCP port (spec ARCH-002).
    address: str
    authkey_path: Path
    lock_path: Path


def supervisor_identity(db_path: Path, *, belay_home: Path | None = None) -> SupervisorIdentity:
    install_id = _install_id(db_path)
    home = belay_home if belay_home is not None else _belay_home()
    if sys.platform == "win32":
        address = f"\\\\.\\pipe\\belay-supervisor-{install_id}"
    else:
        # AF_UNIX path length is limited (~100 bytes on some platforms) -- a
        # short hashed name under a per-user runtime dir stays well within
        # that regardless of how long the real db path is.
        address = str(home / "run" / f"{install_id}.sock")
    return SupervisorIdentity(
        install_id=install_id,
        address=address,
        authkey_path=home / "keys" / f"{install_id}.key",
        lock_path=home / "run" / f"{install_id}.lock",
    )
