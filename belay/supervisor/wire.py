"""JSON-over-`multiprocessing.connection` wire format -- deliberately NOT
`Connection.send()`/`.recv()`, which pickle their argument
(`_ForkingPickler.dumps`/loads under the hood). Python's own
`multiprocessing` docs warn that unpickling data from an untrusted source
"can result in arbitrary code execution" -- and knowing the authkey makes a
peer *authenticated*, not *trusted software*; a compromised or buggy host
process is exactly the kind of peer the threat model has to consider.

`send_bytes`/`recv_bytes` moves the actual byte format under this module's
control instead: canonical UTF-8 JSON, with a hard size cap enforced by
`recv_bytes(maxlength=...)` itself (confirmed empirically: it raises
`OSError` and never buffers a message past the limit, rather than silently
truncating it).
"""

from __future__ import annotations

import json
from typing import Any

#: Generous for any hook payload (structured tool args, a command string) --
#: tight enough that nothing pretending to be a hook event can be used to
#: exhaust memory.
MAX_MESSAGE_BYTES = 1_048_576  # 1 MiB


class WireError(Exception):
    """Malformed, oversized, or otherwise untrustworthy wire data. Callers
    must treat this exactly like `OSError`/`EOFError` -- log and drop the
    connection, never let it propagate and crash the caller."""


def send_json(conn: Any, obj: dict[str, Any]) -> None:
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        # Our own fault, not a hostile peer's -- something this process
        # built is too big to send. Fail loudly rather than truncate.
        raise WireError(f"outgoing message too large: {len(payload)} bytes")
    conn.send_bytes(payload)


def recv_json(conn: Any, *, max_bytes: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    try:
        raw = conn.recv_bytes(maxlength=max_bytes)
    except OSError as exc:
        raise WireError(f"oversized or malformed message: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WireError(f"not valid UTF-8: {exc}") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WireError(f"not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise WireError(f"expected a JSON object at top level, got {type(obj).__name__}")
    return obj
