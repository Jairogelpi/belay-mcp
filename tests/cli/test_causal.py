"""belay/cli/causal.py: assembling the causal graph from ledger events."""

from __future__ import annotations

from belay.cli.causal import build_causal_graph, to_mermaid
from belay.ledger.store import LedgerStore


def _session_with_two_steps() -> tuple[LedgerStore, str]:
    ledger = LedgerStore()
    sid = "s_test"
    ledger.append(
        sid,
        "plan_created",
        {
            "tool": "fs.write_file",
            "args": {"path": "auth.py", "content": "fixed"},
            "intent_id": "auth-fix",
            "test_ref": "tests/test_auth.py::test_login",
            "effects": [{"type": "update", "resource": "fs.file"}],
        },
        step_seq=1,
    )
    ledger.append(sid, "state_captured", {"as": "before", "snapshot": {"value": "old"}}, step_seq=1)
    ledger.append(sid, "policy_evaluated", {"verdict": "allow", "reasons": []}, step_seq=1)
    ledger.append(sid, "step_committed", {"tool": "fs.write_file"}, step_seq=1)
    ledger.append(
        sid,
        "plan_created",
        {"tool": "fs.write_file", "args": {"path": "auth.py", "content": "v2"}},
        step_seq=2,
    )
    ledger.append(sid, "step_committed", {"tool": "fs.write_file"}, step_seq=2)
    return ledger, sid


def test_build_causal_graph_extracts_intent_test_and_capture() -> None:
    ledger, sid = _session_with_two_steps()
    nodes = build_causal_graph(ledger.read(sid))
    assert len(nodes) == 2
    first = nodes[0]
    assert first.intent_id == "auth-fix"
    assert first.test_ref == "tests/test_auth.py::test_login"
    assert first.read_before == {"value": "old"}
    assert first.policy_verdict == "allow"
    assert first.status == "step_committed"


def test_depends_on_same_path_heuristic() -> None:
    """Step 2 touches the same path as step 1 -- inferred dependency."""
    ledger, sid = _session_with_two_steps()
    nodes = build_causal_graph(ledger.read(sid))
    assert nodes[1].depends_on == [1]
    assert nodes[0].depends_on == []


def test_to_mermaid_produces_valid_flowchart_with_intent_and_test_nodes() -> None:
    ledger, sid = _session_with_two_steps()
    nodes = build_causal_graph(ledger.read(sid))
    mermaid = to_mermaid(nodes, sid)
    assert mermaid.startswith("flowchart TD")
    assert "intent: auth-fix" in mermaid
    assert "claimed, never run: tests/test_auth.py::test_login" in mermaid
    assert "S1 --> S2" in mermaid


def test_empty_session_produces_no_nodes() -> None:
    ledger = LedgerStore()
    sid = "s_empty"
    ledger.append(sid, "session_started", {}, step_seq=None)
    nodes = build_causal_graph(ledger.read(sid))
    assert nodes == []
