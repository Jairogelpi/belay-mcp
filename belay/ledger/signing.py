"""Cryptographically signed, offline-verifiable evidence (plan-v2 E13).

Signs a session's ledger events with Ed25519 (`cryptography`, no hand-rolled
crypto) so a third party with no access to Belay's database and no trust
relationship with the operator can verify, from a single self-contained
file plus a public key, that an exact sequence of events really happened and
has not been altered since export.

This is additive over the existing hash chain (E2, `belay/ledger/verify.py`)
-- it reuses `verify_chain`/`verify_coherence`, never recomputes the chain a
second way, and never changes the unsigned `belay verify` path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, Field

from belay.canonical import canonical_bytes
from belay.ledger.model import Event, VerifyReport
from belay.ledger.verify import verify_chain, verify_coherence


class SigningKey:
    """Wrapper around an Ed25519 private key (never stored in the ledger DB).

    Persisted to a file path the operator controls -- PKCS8 PEM, unencrypted.
    Encrypting the file at rest (or holding it in an OS keychain/HSM) is the
    operator's responsibility, out of scope for v1 (see the ADR).
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def generate(cls) -> SigningKey:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def load(cls, path: str | Path) -> SigningKey:
        pem = Path(path).read_bytes()
        private_key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError(f"{path} does not contain an Ed25519 private key")
        return cls(private_key)

    def save(self, path: str | Path) -> None:
        """Write the private key to `path` as unencrypted PKCS8 PEM."""
        pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        Path(path).write_bytes(pem)

    def public_bytes(self) -> bytes:
        return self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_hex(self) -> str:
        return self.public_bytes().hex()

    def sign(self, data: bytes) -> bytes:
        return self._private_key.sign(data)


def _verify_signature(public_key_hex: str, data: bytes, signature_hex: str) -> bool:
    """True iff `signature_hex` is a valid Ed25519 signature of `data` under
    `public_key_hex`. Never raises -- any malformed input is just "not valid"."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public_key.verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError):
        return False


def _signed_summary(bundle_like: Any) -> dict[str, Any]:
    """The exact fields `sign_session` signs -- shared by signing and verification
    so a verifier reconstructs byte-for-byte what was actually signed."""
    return {
        "session_id": bundle_like.session_id,
        "set_hash": bundle_like.set_hash,
        "chain_head_hash": bundle_like.chain_head_hash,
        "event_count": bundle_like.event_count,
        "signed_at": bundle_like.signed_at,
    }


class SignedEvidence(BaseModel):
    """Self-contained, offline-verifiable evidence bundle.

    Embeds the full event list -- a verifier needs nothing else, no database,
    no network. `public_key` is included for convenience/display only; a
    verifier that actually trusts a *specific* key should supply it out of
    band (`verify_evidence(bundle, trusted_public_key_hex=...)`), since an
    attacker who tampers with the file can just as easily replace the
    embedded public key to match a forged signature.
    """

    schema_version: str = "1"
    session_id: str
    set_hash: str | None = None
    chain_head_hash: str
    event_count: int
    signed_at: str
    public_key: str
    signature: str
    events: list[Event]


class VerificationResult(BaseModel):
    """Result of `verify_evidence`, precise about which check failed.

    `stage` is one of `None` (fully valid), `"chain"`, `"coherence"`,
    `"signature"`, or `"summary_mismatch"` -- never a single opaque
    pass/fail, matching `verify_chain`'s existing precision.
    """

    ok: bool
    stage: str | None = None
    errors: list[str] = Field(default_factory=list)
    chain_report: VerifyReport | None = None
    coherence_report: VerifyReport | None = None


def sign_session(events: list[Event], key: SigningKey) -> SignedEvidence:
    """Sign a session's events, producing a self-contained `SignedEvidence` bundle.

    Reuses `verify_chain` to get the chain's terminal hash -- does not
    recompute the chain a second, parallel way. Raises `ValueError` if the
    chain itself is already broken (signing a broken chain would be
    meaningless) or the event list is empty or spans more than one session.
    """
    if not events:
        raise ValueError("cannot sign an empty event list")
    session_id = events[0].session_id
    if any(e.session_id != session_id for e in events):
        raise ValueError("cannot sign events spanning more than one session_id")

    chain_report = verify_chain(events)
    if not chain_report.ok:
        raise ValueError(f"cannot sign a broken chain: {'; '.join(chain_report.errors)}")

    set_hash = next((e.set_hash for e in reversed(events) if e.set_hash is not None), None)
    chain_head_hash = events[-1].hash
    signed_at = datetime.now(UTC).isoformat()
    summary: dict[str, Any] = {
        "session_id": session_id,
        "set_hash": set_hash,
        "chain_head_hash": chain_head_hash,
        "event_count": len(events),
        "signed_at": signed_at,
    }
    signature = key.sign(canonical_bytes(summary)).hex()

    return SignedEvidence(
        session_id=session_id,
        set_hash=set_hash,
        chain_head_hash=chain_head_hash,
        event_count=len(events),
        signed_at=signed_at,
        public_key=key.public_hex(),
        signature=signature,
        events=events,
    )


def verify_evidence(
    bundle: SignedEvidence, trusted_public_key_hex: str | None = None
) -> VerificationResult:
    """Verify a `SignedEvidence` bundle. Pure -- no I/O beyond the bundle passed in.

    Checked in this order, each reported as its own distinguishable failure
    `stage` (per plan-v2 E13):

    1. `chain` -- the hash chain over the embedded events, via `verify_chain`
       (event *k*'s payload was altered post-export -> fails here, at *k*).
    2. `coherence` -- per-step evidence coherence, via `verify_coherence`.
    3. `signature` -- the Ed25519 signature over the bundle's *stated*
       summary fields, against `trusted_public_key_hex` if supplied,
       otherwise the embedded `public_key` (whole-file re-signed with a
       different key, or summary fields edited without re-signing -> fails
       here).
    4. `summary_mismatch` -- the stated summary fields recomputed *from the
       embedded events themselves* (events appended after signing, so the
       signature over the old, untouched summary still checks out, but no
       longer matches what the events actually contain -> fails here).
    """
    chain_report = verify_chain(bundle.events)
    if not chain_report.ok:
        return VerificationResult(
            ok=False, stage="chain", errors=chain_report.errors, chain_report=chain_report
        )

    coherence_report = verify_coherence(bundle.events)
    if not coherence_report.ok:
        return VerificationResult(
            ok=False,
            stage="coherence",
            errors=coherence_report.errors,
            chain_report=chain_report,
            coherence_report=coherence_report,
        )

    pubkey_hex = trusted_public_key_hex if trusted_public_key_hex is not None else bundle.public_key
    stated_summary_bytes = canonical_bytes(_signed_summary(bundle))
    if not _verify_signature(pubkey_hex, stated_summary_bytes, bundle.signature):
        return VerificationResult(
            ok=False,
            stage="signature",
            errors=["Ed25519 signature does not verify against the summary and public key"],
            chain_report=chain_report,
            coherence_report=coherence_report,
        )

    actual_event_count = len(bundle.events)
    actual_chain_head = bundle.events[-1].hash if bundle.events else None
    mismatches: list[str] = []
    if actual_event_count != bundle.event_count:
        mismatches.append(
            f"event_count mismatch: signed summary says {bundle.event_count}, "
            f"embedded events contain {actual_event_count}"
        )
    if actual_chain_head != bundle.chain_head_hash:
        mismatches.append(
            f"chain_head_hash mismatch: signed summary says {bundle.chain_head_hash}, "
            f"recomputed chain head is {actual_chain_head}"
        )
    if mismatches:
        return VerificationResult(
            ok=False,
            stage="summary_mismatch",
            errors=mismatches,
            chain_report=chain_report,
            coherence_report=coherence_report,
        )

    return VerificationResult(ok=True, chain_report=chain_report, coherence_report=coherence_report)
