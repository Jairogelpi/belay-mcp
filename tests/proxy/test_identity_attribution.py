"""Identity attribution: who told the agent to do this (plan-v2 E14).

`Lifecycle.start_session` requires an explicit `initiated_by`; it's bound
once on `session_started` and surfaced session-wide via
`belay.ledger.replay.replay`'s `SessionState` (see ADR 0014 for the
storage-approach choice). E13 signing composes with it via
`belay.ledger.signing.sign_session`/`verify_evidence`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest
from belay.cli.main import app
from belay.contracts.model import ContractSet
from belay.ledger.replay import replay
from belay.ledger.signing import SignedEvidence, SigningKey, sign_session, verify_evidence
from belay.ledger.store import LedgerStore
from belay.proxy.lifecycle import Lifecycle
from hypothesis import given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _empty_contract_set() -> ContractSet:
    return ContractSet(contracts={}, set_hash="sha256:empty")


def _lifecycle(session_id: str, ledger: LedgerStore | None = None) -> Lifecycle:
    return Lifecycle(
        contract_set=_empty_contract_set(),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger or LedgerStore(),
        session_id=session_id,
    )


# -- 1. omission is caught, never silently swallowed -------------------------


def test_start_session_without_initiated_by_is_a_type_error() -> None:
    lifecycle = _lifecycle("s1")
    with pytest.raises(TypeError):
        lifecycle.start_session()  # type: ignore[call-arg]


def test_start_session_with_explicit_unknown_is_the_loud_opt_out() -> None:
    """An unattributed session is allowed, but only as an explicit, visible choice."""
    ledger = LedgerStore()
    lifecycle = _lifecycle("s1", ledger)
    lifecycle.start_session("unknown")

    events = ledger.read("s1")
    started = next(e for e in events if e.type == "session_started")
    assert started.initiated_by == "unknown"


# -- 2. retrievable via replay()'s SessionState for the whole session --------


def test_initiated_by_and_on_behalf_of_retrievable_via_replay_session_state() -> None:
    ledger = LedgerStore()
    lifecycle = _lifecycle("s1", ledger)
    lifecycle.start_session("alice@corp", "scheduler-bot")

    # Later events in the same session don't repeat the identity (bound once).
    ledger.append("s1", "policy_evaluated", {"verdict": "allow"}, step_seq=1)
    ledger.append("s1", "step_committed", {}, step_seq=1)

    state = replay(ledger.read("s1"))

    assert state.initiated_by == "alice@corp"
    assert state.on_behalf_of == "scheduler-bot"


def test_on_behalf_of_defaults_to_none_when_omitted() -> None:
    ledger = LedgerStore()
    lifecycle = _lifecycle("s1", ledger)
    lifecycle.start_session("alice@corp")

    state = replay(ledger.read("s1"))

    assert state.initiated_by == "alice@corp"
    assert state.on_behalf_of is None


# -- 3. E13 tamper detection extends to identity -----------------------------


def _seed_signed_session(session_id: str = "s1", initiated_by: str = "alice@corp") -> list:
    store = LedgerStore()
    lifecycle = _lifecycle(session_id, store)
    lifecycle.start_session(initiated_by, "scheduler-bot")
    store.append(session_id, "step_journaled", {"tool": "crm.delete"}, step_seq=1)
    store.append(session_id, "result_recorded", {"ok": True}, step_seq=1)
    store.append(session_id, "compensation_registered", {"undo": "crm.restore"}, step_seq=1)
    store.append(session_id, "step_committed", {}, step_seq=1)
    return store.read(session_id)


def test_verify_evidence_reports_initiated_by() -> None:
    events = _seed_signed_session()
    bundle = sign_session(events, SigningKey.generate())

    assert bundle.initiated_by == "alice@corp"
    assert bundle.on_behalf_of == "scheduler-bot"

    report = verify_evidence(bundle)
    assert report.ok


def test_tamper_initiated_by_edited_without_resigning_fails_signature() -> None:
    """Regression on E13's tamper pattern: editing `initiated_by` in the
    signed summary without re-signing must be caught exactly like editing
    `event_count`/`chain_head_hash` (test_tamper_c in tests/ledger/test_signing.py)."""
    import json

    events = _seed_signed_session()
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    tampered = json.loads(bundle.model_dump_json())
    tampered["initiated_by"] = "mallory@evil.example"  # edited, signature left as-is
    tampered_bundle = SignedEvidence.model_validate(tampered)

    report = verify_evidence(tampered_bundle)

    assert not report.ok
    assert report.stage == "signature"


def test_tamper_initiated_by_edited_on_the_underlying_event_fails_chain() -> None:
    """Unlike a summary-only edit, editing `initiated_by` on the embedded
    `session_started` event itself changes that event's hash-covered fields
    (E14 promoted it to a named `Event` field, so it's inside the hash, not
    incidental payload) -- caught at the `chain` stage, at index 0, same as
    tampering any other envelope field (test_tamper_a in test_signing.py)."""
    import json

    events = _seed_signed_session()
    key = SigningKey.generate()
    bundle = sign_session(events, key)

    tampered = json.loads(bundle.model_dump_json())
    tampered["events"][0]["initiated_by"] = "mallory@evil.example"
    tampered_bundle = SignedEvidence.model_validate(tampered)

    report = verify_evidence(tampered_bundle)

    assert not report.ok
    assert report.stage == "chain"
    assert report.chain_report is not None
    assert report.chain_report.failed_index == 0


@settings(max_examples=25, deadline=None)
@given(st.text(min_size=1, max_size=20, alphabet=st.characters(blacklist_categories=("Cs",))))
def test_property_any_tampered_initiated_by_is_detected(forged_identity: str) -> None:
    import json

    events = _seed_signed_session(session_id="s_prop")
    key = SigningKey.generate()
    bundle = sign_session(events, key)
    if forged_identity == bundle.initiated_by:
        return

    tampered = json.loads(bundle.model_dump_json())
    tampered["initiated_by"] = forged_identity
    tampered_bundle = SignedEvidence.model_validate(tampered)

    report = verify_evidence(tampered_bundle)
    assert not report.ok


# -- 4. real CLI: wrap --initiated-by, run, a real stdio call, verify-evidence


@pytest.mark.slow
def test_cli_wrap_run_verify_evidence_surfaces_initiated_by(tmp_path: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db_path = tmp_path / "belay.db"
    config_path = tmp_path / "belay.wrap.json"

    wrap_result = runner.invoke(
        app,
        [
            "wrap",
            str(REPO_ROOT / "examples" / "crm-mock"),
            "--contracts",
            str(REPO_ROOT / "examples" / "contracts" / "crm.yaml"),
            "--db",
            str(db_path),
            "--out",
            str(config_path),
            "--initiated-by",
            "alice@corp",
        ],
    )
    assert wrap_result.exit_code == 0, wrap_result.output

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "belay.cli.main", "run", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
    )

    async def _call() -> None:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "crm.import_records", {"records": {"r1": {"last_seen": 2024}}}
            )
            assert not result.isError, result.content

    anyio.run(_call)

    ledger = LedgerStore(f"sqlite:///{db_path.resolve().as_posix()}")
    all_events = ledger.read_all()
    session_id = all_events[0].session_id

    key_path = tmp_path / "signing.key"
    keygen_result = runner.invoke(app, ["keygen", str(key_path)])
    assert keygen_result.exit_code == 0

    evidence_path = tmp_path / "evidence.json"
    export_result = runner.invoke(
        app,
        [
            "verify-export",
            session_id,
            "--key",
            str(key_path),
            "--db",
            str(db_path),
            "-o",
            str(evidence_path),
        ],
    )
    assert export_result.exit_code == 0, export_result.output

    verify_result = runner.invoke(app, ["verify-evidence", str(evidence_path)])

    assert verify_result.exit_code == 0, verify_result.output
    assert "alice@corp" in verify_result.stdout


# -- 5. multiple sessions from different initiators never cross-contaminate --


@settings(max_examples=15, deadline=None)
@given(st.integers(min_value=2, max_value=8))
def test_property_n_sessions_from_different_initiators_never_cross_contaminate(n: int) -> None:
    ledger = LedgerStore("sqlite:///:memory:")
    identities = [f"user{i}@corp" for i in range(n)]

    for i, identity in enumerate(identities):
        session_id = f"s_{i}"
        lifecycle = _lifecycle(session_id, ledger)
        lifecycle.start_session(identity)
        ledger.append(session_id, "step_committed", {}, step_seq=1)

    for i, identity in enumerate(identities):
        session_id = f"s_{i}"
        state = replay(ledger.read(session_id))
        assert state.initiated_by == identity
        # No other identity leaked into this session's events.
        for ev in ledger.read(session_id):
            if ev.initiated_by is not None:
                assert ev.initiated_by == identity
