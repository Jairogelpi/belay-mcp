"""Field-level redaction of ledger payloads (spec §9.3).

@spec("9.3")
"""

from __future__ import annotations

import copy
from typing import Any, Protocol

from belay.canonical import canonical_bytes, sha256_hex

# Fixed salt: equal cleartext values MUST redact to equal hashes (spec §9.3:
# "equality remains checkable"), which rules out a per-call random salt.
_REDACTION_SALT = b"belay-ledger-redaction-v1"


class _HasRedact(Protocol):
    redact: list[str] | None


def _hash_value(value: Any) -> str:
    return "sha256:" + sha256_hex(_REDACTION_SALT + canonical_bytes(value))


def _redact_path(payload: dict[str, Any], path: str) -> None:
    """Apply one `$args.foo.bar`-style path (spec §4.3 expression paths)."""
    parts = [p for p in path.lstrip("$").split(".") if p]
    if not parts:
        return
    node: Any = payload
    for key in parts[:-1]:
        if not isinstance(node, dict) or key not in node:
            return
        node = node[key]
    last = parts[-1]
    if isinstance(node, dict) and last in node:
        node[last] = {"redacted": True, "hash": _hash_value(node[last])}


def redact(payload: dict[str, Any], contract: _HasRedact | None) -> dict[str, Any]:
    """Redact `contract.redact` paths in a copy of `payload` (spec §9.3).

    Redacted fields become `{"redacted": true, "hash": "sha256:..."}`: the
    cleartext never appears, but two events redacting the same secret hash
    identically, so equality is still checkable.
    """
    paths = getattr(contract, "redact", None) if contract is not None else None
    if not paths:
        return payload
    result = copy.deepcopy(payload)
    for path in paths:
        _redact_path(result, path)
    return result
