"""End-to-end: belay:test_verified events -> belay causal's VERIFIED/CLAIMED/FAILED tiers.

Covers the exact attack a second review round demonstrated: a fake
_belay_test_ref plus an unrelated --cmd that happens to pass must never
show as VERIFIED.
"""

from __future__ import annotations

from belay.cli.causal import build_causal_graph, to_mermaid
from belay.ledger.store import LedgerStore


def _step_with_test_ref(ledger: LedgerStore, sid: str, step_seq: int, test_ref: str) -> None:
    ledger.append(
        sid,
        "plan_created",
        {"tool": "fs.write_file", "args": {"path": f"f{step_seq}.py"}, "test_ref": test_ref},
        step_seq=step_seq,
    )
    ledger.append(sid, "step_committed", {"tool": "fs.write_file"}, step_seq=step_seq)


def test_real_test_verification_shows_as_verified() -> None:
    ledger = LedgerStore()
    sid = "s_1"
    _step_with_test_ref(ledger, sid, 1, "tests/test_a.py::test_x")
    ledger.append(
        sid,
        "belay:test_verified",
        {
            "mode": "test",
            "cmd": "pytest tests/test_a.py::test_x",
            "test_ref": "tests/test_a.py::test_x",
            "exit_code": 0,
            "output_hash": "sha256:abc",
            "duration_ms": 100,
            "passed": True,
        },
        step_seq=1,
    )
    nodes = build_causal_graph(ledger.read(sid))
    assert nodes[0].test_verified is True


def test_fake_test_ref_plus_passing_arbitrary_command_never_shows_verified() -> None:
    """The exact attack: agent declares tests/fake.py::test_ok, operator runs
    `belay verify-test --cmd "python -c pass"` (mode=command) -- must show
    as claimed/unverified, never VERIFIED, regardless of that command's
    exit code."""
    ledger = LedgerStore()
    sid = "s_2"
    _step_with_test_ref(ledger, sid, 1, "tests/fake.py::test_ok")
    ledger.append(
        sid,
        "belay:test_verified",
        {
            "mode": "command",  # NOT "test" -- this is the key distinction
            "cmd": "python -c \"pass\"",
            "test_ref": None,
            "exit_code": 0,
            "output_hash": "sha256:def",
            "duration_ms": 50,
            "passed": True,
        },
        step_seq=1,
    )
    nodes = build_causal_graph(ledger.read(sid))
    assert nodes[0].test_verified is None
    assert nodes[0].test_ref == "tests/fake.py::test_ok"


def test_evidence_for_different_test_ref_than_currently_declared_is_ignored() -> None:
    """Defense in depth: even a mode="test" event is ignored if its test_ref
    doesn't match what the step currently declares (stale evidence)."""
    ledger = LedgerStore()
    sid = "s_3"
    _step_with_test_ref(ledger, sid, 1, "tests/test_a.py::test_x")
    ledger.append(
        sid,
        "belay:test_verified",
        {
            "mode": "test",
            "cmd": "pytest tests/test_OTHER.py::test_y",
            "test_ref": "tests/test_OTHER.py::test_y",  # different ref
            "exit_code": 0,
            "output_hash": "sha256:xyz",
            "duration_ms": 10,
            "passed": True,
        },
        step_seq=1,
    )
    nodes = build_causal_graph(ledger.read(sid))
    assert nodes[0].test_verified is None


def test_failed_real_test_verification_shows_as_failed() -> None:
    ledger = LedgerStore()
    sid = "s_4"
    _step_with_test_ref(ledger, sid, 1, "tests/test_a.py::test_x")
    ledger.append(
        sid,
        "belay:test_verified",
        {
            "mode": "test",
            "cmd": "pytest tests/test_a.py::test_x",
            "test_ref": "tests/test_a.py::test_x",
            "exit_code": 1,
            "output_hash": "sha256:abc",
            "duration_ms": 100,
            "passed": False,
        },
        step_seq=1,
    )
    nodes = build_causal_graph(ledger.read(sid))
    assert nodes[0].test_verified is False


def test_mermaid_distinguishes_verified_claimed_and_failed() -> None:
    ledger = LedgerStore()
    sid = "s_5"
    _step_with_test_ref(ledger, sid, 1, "tests/verified.py::test_a")
    ledger.append(
        sid,
        "belay:test_verified",
        {
            "mode": "test",
            "cmd": "pytest tests/verified.py::test_a",
            "test_ref": "tests/verified.py::test_a",
            "exit_code": 0,
            "output_hash": "sha256:1",
            "duration_ms": 1,
            "passed": True,
        },
        step_seq=1,
    )
    _step_with_test_ref(ledger, sid, 2, "tests/claimed.py::test_b")
    nodes = build_causal_graph(ledger.read(sid))
    mermaid = to_mermaid(nodes, sid)
    assert "VERIFIED: tests/verified.py::test_a" in mermaid
    assert "claimed, never run: tests/claimed.py::test_b" in mermaid
    assert "==proves==>" in mermaid
    assert "claims (unverified)" in mermaid
