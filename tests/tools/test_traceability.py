"""Tests for scripts/traceability.py -- the traceability matrix generator itself.

This is what makes docs/plan.md §8 non-decorative: a red test proves the
generator actually detects an uncovered MUST, not just that it prints "OK".
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import traceability  # noqa: E402


def test_real_repo_has_zero_uncovered_musts() -> None:
    """The current, real test suite covers every hand-curated MUST (this is
    the state the traceability entrega must leave the repo in)."""
    coverage = traceability.scan_markers()
    _, uncovered = traceability.build_report(traceability.MUSTS, coverage)
    assert uncovered == [], f"uncovered MUSTs: {[m.id for m in uncovered]}"


def test_detects_an_uncovered_must_injected_with_no_marker() -> None:
    """Inject a fake MUST with no @spec marker anywhere; the script MUST flag it."""
    coverage = traceability.scan_markers()
    fake = traceability.Must("99.9", "99", "A fake requirement nothing covers.")
    musts_with_fake = [*traceability.MUSTS, fake]

    _, uncovered = traceability.build_report(musts_with_fake, coverage)

    assert [m.id for m in uncovered] == ["99.9"]


def test_passes_when_all_musts_have_at_least_one_marker() -> None:
    """A synthetic MUST list where every id has a marker in a synthetic
    coverage map reports zero uncovered, regardless of the real repo state."""
    musts = [traceability.Must("1.1", "1", "text"), traceability.Must("1.2", "1", "text 2")]
    coverage = {"1.1": ["a.py::test_a"], "1.2": ["b.py::test_b"]}

    _, uncovered = traceability.build_report(musts, coverage)

    assert uncovered == []


def test_generated_table_is_well_formed_and_matches_curated_list() -> None:
    coverage = traceability.scan_markers()
    table, _ = traceability.build_report(traceability.MUSTS, coverage)

    lines = table.splitlines()
    assert lines[0] == "# Traceability matrix"
    header_idx = next(i for i, line in enumerate(lines) if line.startswith("| Spec"))
    assert lines[header_idx + 1].startswith("|---")

    data_rows = [
        line for line in lines[header_idx + 2 :] if line.startswith("|") and line.strip() != "|"
    ]
    assert len(data_rows) == len(traceability.MUSTS)
    for must in traceability.MUSTS:
        assert any(must.text in row for row in data_rows), f"missing row for {must.id}"


def test_main_exits_zero_and_writes_the_doc() -> None:
    import subprocess

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "traceability.py"), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "traceability OK" in result.stdout
