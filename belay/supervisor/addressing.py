"""Per-installation supervisor identity: where it listens, where its
capability token lives, and where its *authoritative* approvals/idempotency
data lives -- all derived deterministically from the project-anchor path a
`belay hooks install` was pointed at (its `--db` option; despite the name,
that value is only ever used as an identity seed now, never opened directly
as a database file -- see `data_path` below), so each project/install gets
its own independent supervisor process, capability, and storage rather than
one global daemon shared across unrelated projects (ARCH-003:
"installation-scoped capability" -- see
docs/adr/0020-extended-requirement-catalog.md).
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
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
    here (ARCH-003/004: not a project file, not solely an env var).
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
    #: never an unauthenticated TCP port (ARCH-002).
    address: str
    authkey_path: Path
    lock_path: Path
    #: The REAL approvals/idempotency SQLite file. Always under
    #: `belay_home()`, never inside the project -- an agent with ordinary
    #: project-directory write access (Edit/Write, or a Bash command this
    #: gate itself allowed) must not be able to reach it directly and, say,
    #: flip a `pending` row to `approved` by hand.
    data_path: Path
    #: A one-line pointer file (plain text: the absolute path to a
    #: contracts YAML/JSON) written by `belay hooks install --contracts
    #: <file>`. Its presence is what turns on the R1-first-slice check in
    #: `belay/hooks/gate.py::evaluate_file_edit`: native `Edit`/`Write`/
    #: `NotebookEdit` calls get resolved against the real `ContractSet` the
    #: same way the MCP proxy's `resolve()` does, instead of always
    #: allowing by default. Absent (the default) means fully unchanged
    #: behavior -- this is opt-in, not a silent tightening for existing
    #: installs. Stored under `belay_home()`, same private-storage rule as
    #: everything else here.
    contracts_pointer_path: Path


def supervisor_identity(
    project_anchor: Path, *, belay_home: Path | None = None
) -> SupervisorIdentity:
    install_id = _install_id(project_anchor)
    home = belay_home if belay_home is not None else _default_belay_home()
    if sys.platform == "win32":
        address = f"\\\\.\\pipe\\belay-supervisor-{install_id}"
    else:
        # AF_UNIX path length is limited (~104 bytes on macOS, ~108 on
        # Linux). `belay_home()` (e.g. `~/.belay`) is normally short enough
        # -- but a long real-world home directory, or (confirmed for real
        # via macOS CI, plan-v2 E19.7 -- OSError: AF_UNIX path too long) a
        # test's own belay_home override under pytest's tmp_path, can blow
        # past that; macOS's own $TMPDIR is notorious for exactly this. The
        # system temp directory is kept deliberately short by the OS for
        # this exact reason -- the conventional place for a Unix domain
        # socket -- so the socket lives there instead, namespaced by both
        # OS user (a shared /tmp on a multi-user machine must not let one
        # user's install collide with another's -- home-directory-based
        # paths got this for free via $HOME, this needs it explicitly) and
        # install_id. The lock/data/authkey files (ordinary file opens, no
        # such length limit) stay under belay_home() as before -- this
        # change is scoped to the socket path alone.
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.getuid())
        address = str(Path(tempfile.gettempdir()) / f"belay-{user}-{install_id}.sock")
    return SupervisorIdentity(
        install_id=install_id,
        address=address,
        authkey_path=home / "keys" / f"{install_id}.key",
        lock_path=home / "run" / f"{install_id}.lock",
        data_path=home / "data" / f"{install_id}.db",
        contracts_pointer_path=home / "data" / f"{install_id}.contracts_path.txt",
    )
