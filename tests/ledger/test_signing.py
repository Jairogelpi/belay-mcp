"""Signed, offline-verifiable evidence (plan-v2 E13).

`sign_session`/`verify_evidence` reuse `verify_chain`/`verify_coherence` (E2)
and `belay/canonical.py` -- no parallel chain-recomputation, no second
canonicalization. These tests cover the four tamper scenarios from
plan-v2's E13 section, each asserting the *specific* failure stage reported.
"""

from __future__ import annotations

import json

import pytest
from belay.ledger.model import Event
from belay.ledger.signing import SignedEvidence, SigningKey, sign_session, verify_evidence
from belay.ledger.store import LedgerStore
from cryptography.hazmat.primitives import serialization
from hypothesis import given, settings
from hypothesis import strategies as st


def _seed_session(store: LedgerStore, session_id: str = "s1") -> list[Event]:
    store.append(session_id, "session_started", {"agent": "demo"})
    store.append(session_id, "step_journaled", {"tool": "crm.delete"}, step_seq=1)
    store.append(session_id, "result_recorded", {"ok": True}, step_seq=1)
    store.append(session_id, "compensation_registered", {"undo": "crm.restore"}, step_seq=1)
    store.append(session_id, "step_committed", {}, step_seq=1)
    return store.read(session_id)


def test_sign_then_verify_a_real_multi_event_session_is_fully_valid() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    key = SigningKey.generate()

    bundle = sign_session(events, key)
    report = verify_evidence(bundle)

    assert report.ok
    assert report.stage is None
    assert report.errors == []


def test_tamper_a_event_payload_byte_flipped_fails_chain_at_the_right_index() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    tampered = json.loads(bundle.model_dump_json())
    tampered["events"][1]["payload"]["tool"] = "crm.DELETE"  # flip a byte in event index 1
    tampered_bundle = SignedEvidence.model_validate(tampered)

    report = verify_evidence(tampered_bundle)

    assert not report.ok
    assert report.stage == "chain"
    assert report.chain_report is not None
    assert report.chain_report.failed_index == 1


def test_tamper_b_file_resigned_with_a_different_key_fails_signature() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    original_key = SigningKey.generate()
    bundle = sign_session(events, original_key)

    attacker_key = SigningKey.generate()
    forged = sign_session(events, attacker_key)  # same events, re-signed with a different key

    # Verifier trusts the *original* operator's public key, supplied out of band --
    # not whatever public key the (possibly-replaced) file itself claims.
    report = verify_evidence(forged, trusted_public_key_hex=bundle.public_key)

    assert not report.ok
    assert report.stage == "signature"


def test_tamper_c_summary_field_edited_without_resigning_fails_signature() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    tampered = json.loads(bundle.model_dump_json())
    tampered["event_count"] = tampered["event_count"] + 1  # edited, signature left as-is
    tampered_bundle = SignedEvidence.model_validate(tampered)

    report = verify_evidence(tampered_bundle)

    assert not report.ok
    assert report.stage == "signature"


def test_tamper_d_events_appended_after_signing_fails_summary_mismatch() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    extra = store.append("s1", "session_closed", {}, step_seq=1)  # appended post-signing

    tampered = json.loads(bundle.model_dump_json())
    tampered["events"].append(json.loads(extra.model_dump_json()))
    tampered_bundle = SignedEvidence.model_validate(tampered)

    report = verify_evidence(tampered_bundle)

    assert not report.ok
    assert report.stage == "summary_mismatch"
    assert any("event_count" in e for e in report.errors)


def test_wrong_public_key_supplied_fails_signature_cleanly_no_crash() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    wrong_key = SigningKey.generate()
    report = verify_evidence(bundle, trusted_public_key_hex=wrong_key.public_hex())

    assert not report.ok
    assert report.stage == "signature"


def test_private_key_never_appears_in_the_exported_evidence_bytes() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    raw = bundle.model_dump_json().encode("utf-8")

    private_raw = key._private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    assert private_raw not in raw
    assert private_raw.hex().encode("ascii") not in raw
    assert b"PRIVATE KEY" not in raw
    # only the public key/fingerprint appears
    assert key.public_hex().encode("ascii") in raw


def test_regression_verify_chain_and_verify_coherence_unaffected_by_signing() -> None:
    """E2's verify_chain/verify_coherence keep working exactly as before --
    signing is purely additive, never invoked unless opted into."""
    from belay.ledger.verify import verify_chain, verify_coherence

    store = LedgerStore()
    events = _seed_session(store)

    assert verify_chain(events).ok
    assert verify_coherence(events).ok


@settings(max_examples=50, deadline=None)
@given(st.integers(min_value=0, max_value=10_000))
def test_property_flipping_any_single_byte_in_events_always_fails_verification(seed: int) -> None:
    """Hypothesis: for any valid signed bundle, flipping any single byte in the
    embedded event payloads' serialized bytes always fails verification --
    never a false negative. Core security guarantee of E13."""
    store = LedgerStore("sqlite:///:memory:")
    events = _seed_session(store, session_id=f"s{seed}")
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    raw = bundle.model_dump_json()
    # pick a byte position inside the events array specifically (not headers/signature)
    events_start = raw.index('"events"')
    if len(raw) <= events_start + 1:
        return
    import random

    rng = random.Random(seed)
    pos = rng.randrange(events_start, len(raw))
    original_char = raw[pos]
    # flip to something different, staying printable-safe enough for a byte flip
    replacement = "X" if original_char != "X" else "Y"
    mutated_raw = raw[:pos] + replacement + raw[pos + 1 :]

    if mutated_raw == raw:
        return

    try:
        mutated_bundle = SignedEvidence.model_validate_json(mutated_raw)
    except Exception:
        # mutation broke JSON/schema itself -- that is also a legitimate failure
        # to verify (the file is unusable), consistent with "never a false negative"
        return

    report = verify_evidence(mutated_bundle)
    assert not report.ok, f"byte flip at {pos} in events was not detected"


def test_verify_evidence_roundtrip_with_no_belay_db_present(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Prove the 'no installation needed' claim for real: verify_evidence needs
    only the exported file, no belay.db anywhere near it."""
    store = LedgerStore()
    events = _seed_session(store)
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text(bundle.model_dump_json(indent=2), encoding="utf-8")

    assert not (tmp_path / "belay.db").exists()
    assert list(tmp_path.glob("*.db")) == []

    loaded = SignedEvidence.model_validate_json(evidence_file.read_text(encoding="utf-8"))
    report = verify_evidence(loaded, trusted_public_key_hex=key.public_hex())

    assert report.ok


def test_sign_session_rejects_an_empty_event_list() -> None:
    with pytest.raises(ValueError):
        sign_session([], SigningKey.generate())


def test_sign_session_rejects_a_broken_chain() -> None:
    store = LedgerStore()
    events = _seed_session(store)
    events[1].hash = "deadbeef" * 8  # break the chain in memory before signing

    with pytest.raises(ValueError, match="broken chain"):
        sign_session(events, SigningKey.generate())
