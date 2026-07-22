"""Canonical JSON serialization and hashing (spec §4.7, §9.1, §9.2).

Canonical form: JSON with keys sorted, no insignificant whitespace, UTF-8.
Used as the basis of `set_hash` (§4.7) and the ledger's evidence hash chain
(§9.2). Anything hashed for evidence purposes MUST go through this module so
two structurally-equal payloads (regardless of key order or origin format —
YAML vs JSON) always hash identically.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    """Serialize `obj` to canonical JSON bytes: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def canonical_json(obj: Any) -> str:
    """Serialize `obj` to a canonical JSON string."""
    return canonical_bytes(obj).decode("utf-8")


def sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj: Any) -> str:
    """SHA-256 hex digest of `obj`'s canonical JSON form."""
    return sha256_hex(canonical_bytes(obj))
