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
import time
from pathlib import Path

TOKEN_LENGTH = 32

#: How long a "loser" of the create race waits for the winner to finish
#: writing before giving up. Writing 32 bytes is not something that should
#: ever take anywhere near this long -- it's a generous ceiling against a
#: genuinely stuck/crashed winner, not a normal-case budget.
_READ_RACE_TIMEOUT_S = 2.0


def _read_complete_token(path: Path, *, timeout: float = _READ_RACE_TIMEOUT_S) -> bytes:
    """Read `path`, retrying until it holds a full token. Guards a real race:
    `O_CREAT | O_EXCL` makes file *creation* atomic, but a second process
    that loses that race and immediately reads the file can still observe
    it as empty (created, not yet written) -- confirmed empirically:
    `os.fdopen(fd, "wb").write(...)` is not instantaneous from an outside
    reader's perspective, so a bare `read_bytes()` right after `FileExistsError`
    can return `b""` instead of the winner's 32 bytes."""
    deadline = time.monotonic() + timeout
    data = path.read_bytes()
    while len(data) < TOKEN_LENGTH and time.monotonic() < deadline:
        time.sleep(0.005)
        data = path.read_bytes()
    return data


def load_or_create_authkey(path: Path) -> bytes:
    """Read the existing token, or atomically create one if this is the
    first process to ever need it. `O_CREAT | O_EXCL` (not a check-then-write)
    so two processes racing on the very first call can't each generate a
    different token and clobber one another -- the loser reads back
    whatever the winner wrote, waiting for that write to actually land
    (see `_read_complete_token`) rather than racing it."""
    if path.is_file():
        return _read_complete_token(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_complete_token(path)
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(TOKEN_LENGTH))
    return path.read_bytes()
