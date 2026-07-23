"""Field-level redaction (spec §9.3)."""

from __future__ import annotations

from types import SimpleNamespace

from belay.ledger.redact import redact


def test_redacted_field_never_appears_in_cleartext() -> None:
    """@spec("9.3") — implementations MUST support field-level redaction at write time."""
    contract = SimpleNamespace(redact=["$args.password"])
    payload = {"args": {"password": "hunter2", "user": "jairo"}}

    redacted = redact(payload, contract)

    assert redacted["args"]["user"] == "jairo"
    assert redacted["args"]["password"] != "hunter2"
    assert "hunter2" not in str(redacted)
    assert redacted["args"]["password"]["redacted"] is True


def test_equal_secrets_redact_to_equal_hashes() -> None:
    contract = SimpleNamespace(redact=["$args.password"])
    p1 = redact({"args": {"password": "hunter2"}}, contract)
    p2 = redact({"args": {"password": "hunter2"}}, contract)

    assert p1["args"]["password"]["hash"] == p2["args"]["password"]["hash"]


def test_different_secrets_redact_to_different_hashes() -> None:
    contract = SimpleNamespace(redact=["$args.password"])
    p1 = redact({"args": {"password": "hunter2"}}, contract)
    p2 = redact({"args": {"password": "different"}}, contract)

    assert p1["args"]["password"]["hash"] != p2["args"]["password"]["hash"]


def test_redact_leaves_payload_untouched_when_contract_has_no_redact_list() -> None:
    payload = {"args": {"password": "hunter2"}}
    assert redact(payload, None) == payload
    assert redact(payload, SimpleNamespace(redact=None)) == payload


def test_redact_does_not_mutate_input_payload() -> None:
    contract = SimpleNamespace(redact=["$args.password"])
    payload = {"args": {"password": "hunter2"}}
    redact(payload, contract)
    assert payload["args"]["password"] == "hunter2"
