"""Per-installation supervisor identity: where it listens, where its
capability token lives, and where its *authoritative* approvals/idempotency
data lives -- all derived deterministically from the project-anchor path a
`belay hooks install` was pointed at (its `--db` option; despite the name,
that value is only ever used as an identity seed now, never opened directly
as a database file -- see `data_path` below), so each project/install gets
its own independent supervisor process, capability, and storage rather than
one global daemon shared across unrelated projects (spec ARCH-003:
"installation-scoped capability").
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _default_belay_home() -> Path:
    # BELAY_HOME override: mainly so tests (and this repo's own end-to-end
    # CLI tests, which invoke `belay` as a real subprocess/CliRunner rather
    # than constructing a SupervisorIdentity directly and can't just pass
    # belay_home=... as a function argument) never write into a real
    # developer's actual home directory -- confirmed necessary the hard
    # way: running this suite without it left dozens of real key/data files
    # under this machine's actual %LOCALAPPDATA%\belay\. Also a legitimate
    # feature for real users who want belay's private state somewhere
    # non-default (a managed-install policy, an unusual filesystem layout).
    override = os.environ.get("BELAY_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "belay"
    return Path.home() / ".belay"


def belay_home() -> Path:
    """User-scoped, outside any project directory -- an agent restricted to
    project-directory tool calls cannot read or tamper with anything stored
    here (spec ARCH-003/004: not a project file, not solely an env var).
    Everything privacy/security-sensitive this package owns (capability
    tokens, spawn locks, and -- since a P0 review found the authoritative
    approvals database sitting inside the project it gates, defeating the
    whole point -- the approvals/idempotency database itself) lives under
    here, never under a project directory the gated agent can write to.
    Overridable via the `BELAY_HOME` environment variable."""
    return _default_belay_home()


def _install_id(project_anchor: Path) -> str:
    return hashlib.sha256(str(project_anchor.resolve()).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SupervisorIdentity:
    install_id: str
    #: `multiprocessing.connection` address: a Windows named-pipe path
    #: (`\\.\pipe\...`) on win32, a Unix domain socket path elsewhere --
    #: never an unauthenticated TCP port (spec ARCH-002).
    address: str
    authkey_path: Path
    lock_path: Path
    #: The REAL approvals/idempotency SQLite file. Always under
    #: `belay_home()`, never inside the project -- an agent with ordinary
    #: project-directory write access (Edit/Write, or a Bash command this
    #: gate itself allowed) must not be able to reach it directly and, say,
    #: flip a `pending` row to `approved` by hand.
    data_path: Path


def supervisor_identity(
    project_anchor: Path, *, belay_home: Path | None = None
) -> SupervisorIdentity:
    install_id = _install_id(project_anchor)
    home = belay_home if belay_home is not None else _default_belay_home()
    if sys.platform == "win32":
        address = f"\\\\.\\pipe\\belay-supervisor-{install_id}"
    else:
        # AF_UNIX path length is limited (~100 bytes on some platforms) -- a
        # short hashed name under a per-user runtime dir stays well within
        # that regardless of how long the real project path is.
        address = str(home / "run" / f"{install_id}.sock")
    return SupervisorIdentity(
        install_id=install_id,
        address=address,
        authkey_path=home / "keys" / f"{install_id}.key",
        lock_path=home / "run" / f"{install_id}.lock",
        data_path=home / "data" / f"{install_id}.db",
    )
