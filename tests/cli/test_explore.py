"""belay/cli/explore.py: side-by-side variant comparison metrics."""

from __future__ import annotations

from belay.cli.explore import compute_metrics
from belay.ledger.store import LedgerStore


def test_compute_metrics_counts_files_and_proven_steps() -> None:
    ledger = LedgerStore()
    sid = "s_a"
    ledger.append(
        sid,
        "plan_created",
        {
            "tool": "fs.write_file",
            "args": {"path": "a.py"},
            "test_ref": "tests/test_a.py::test_x",
        },
        step_seq=1,
    )
    ledger.append(sid, "step_committed", {"tool": "fs.write_file"}, step_seq=1)
    ledger.append(
        sid, "plan_created", {"tool": "fs.write_file", "args": {"path": "b.py"}}, step_seq=2
    )
    ledger.append(sid, "step_committed", {"tool": "fs.write_file"}, step_seq=2)

    metrics = compute_metrics(sid, ledger.read(sid), rewind_irreversible_count=0)
    assert metrics.steps_total == 2
    assert metrics.files_touched == 2
    assert metrics.steps_proven == 1
    assert metrics.steps_unproven == 1
    assert metrics.tools_used == ["fs.write_file"]
    assert metrics.irreversible_or_indeterminate == 0


def test_compute_metrics_counts_unknown_effects() -> None:
    ledger = LedgerStore()
    sid = "s_b"
    ledger.append(
        sid,
        "plan_created",
        {"tool": "crm.bulk_delete", "args": {}, "unknown": [{"reason": "no estimate"}]},
        step_seq=1,
    )
    metrics = compute_metrics(sid, ledger.read(sid), rewind_irreversible_count=None)
    assert metrics.unknown_effects == 1
    assert metrics.irreversible_or_indeterminate is None
