"""Installation-scoped capability token (spec ARCH-003/004): a random secret,
generated once, stored outside the project directory with restrictive
permissions -- never a plain environment variable as the sole gate, since an
agent can set environment variables itself. Both the supervisor server and
every client load the same token from the same file; whichever process gets
there first creates it.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


def load_or_create_authkey(path: Path) -> bytes:
    """Read the existing token, or atomically create one if this is the
    first process to ever need it. `O_CREAT | O_EXCL` (not a check-then-write)
    so two processes racing on the very first call can't each generate a
    different token and clobber one another -- the loser just reads back
    whatever the winner wrote."""
    if path.is_file():
        return path.read_bytes()

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_bytes()
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(32))
    return path.read_bytes()
