"""Installation-scoped capability token (ARCH-003/004, see
docs/adr/0020-extended-requirement-catalog.md): a random secret,
generated once, stored outside the project directory with restrictive
permissions -- never a plain environment variable as the sole gate, since an
agent can set environment variables itself. Both the supervisor server and
every client load the same token from the same file; whichever process gets
there first creates it.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path

logger = logging.getLogger("belay.supervisor")

TOKEN_LENGTH = 32

#: How long a "loser" of the create race waits for the winner to finish
#: writing before giving up. Writing 32 bytes is not something that should
#: ever take anywhere near this long -- it's a generous ceiling against a
#: genuinely stuck/crashed winner, not a normal-case budget.
_READ_RACE_TIMEOUT_S = 2.0
#: Bounds recursive corruption-recovery attempts (see `load_or_create_authkey`)
#: so a persistently unreadable/unwritable path fails loudly instead of
#: recursing forever.
_MAX_REPAIR_ATTEMPTS = 3


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


def load_or_create_authkey(
    path: Path, *, _repair_attempts_left: int = _MAX_REPAIR_ATTEMPTS
) -> bytes:
    """Read the existing token, or atomically create one if this is the
    first process to ever need it. `O_CREAT | O_EXCL` (not a check-then-write)
    so two processes racing on the very first call can't each generate a
    different token and clobber one another -- the loser reads back
    whatever the winner wrote, waiting for that write to actually land
    (see `_read_complete_token`) rather than racing it.

    A token file found with the wrong length (truncated by a partial write
    that never completed, corrupted, or replaced by something else entirely
    -- e.g. a symlink to an unrelated file) is never silently used as-is:
    that would authenticate with a weaker-than-intended, or simply wrong,
    key. It's discarded and regenerated instead, same as if it never
    existed. Raises `RuntimeError` if that can't be made to converge within
    `_MAX_REPAIR_ATTEMPTS` (a persistently unwritable path, for example) --
    a supervisor or client that can't establish a real capability token
    must not fall back to running unauthenticated.
    """
    if path.is_file():
        data = _read_complete_token(path)
        if len(data) == TOKEN_LENGTH:
            return data
        if _repair_attempts_left <= 0:
            raise RuntimeError(
                f"capability token at {path} is corrupted ({len(data)} bytes, expected "
                f"{TOKEN_LENGTH}) and could not be repaired"
            )
        logger.warning(
            "capability token at %s is corrupted (%d bytes, expected %d) -- regenerating",
            path,
            len(data),
            TOKEN_LENGTH,
        )
        path.unlink(missing_ok=True)
        return load_or_create_authkey(path, _repair_attempts_left=_repair_attempts_left - 1)

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Someone else just created it -- re-enter so the read above (with
        # its corruption check) is what actually loads it, rather than
        # duplicating that logic here.
        return load_or_create_authkey(path, _repair_attempts_left=_repair_attempts_left)
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(TOKEN_LENGTH))
    written = path.read_bytes()
    if len(written) != TOKEN_LENGTH:
        # Same corruption check applied to what we JUST wrote ourselves --
        # belt and suspenders against a broken read/write path underneath
        # this function, not just a pre-existing bad file.
        if _repair_attempts_left <= 0:
            raise RuntimeError(
                f"just-written capability token at {path} read back as {len(written)} "
                f"bytes, expected {TOKEN_LENGTH} -- refusing to use it"
            )
        path.unlink(missing_ok=True)
        return load_or_create_authkey(path, _repair_attempts_left=_repair_attempts_left - 1)
    return written
