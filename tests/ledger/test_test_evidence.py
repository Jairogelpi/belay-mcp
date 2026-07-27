"""belay/ledger/test_evidence.py: real command execution and outcome recording."""

from __future__ import annotations

from belay.ledger.test_evidence import run_test


def test_passing_command_recorded_as_passed() -> None:
    result = run_test("python -c \"print('ok')\"")
    assert result.passed is True
    assert result.exit_code == 0
    assert result.output_hash.startswith("sha256:")
    assert result.duration_ms >= 0


def test_failing_command_recorded_as_failed() -> None:
    result = run_test("python -c \"import sys; sys.exit(1)\"")
    assert result.passed is False
    assert result.exit_code == 1


def test_output_hash_differs_for_different_output() -> None:
    a = run_test("python -c \"print('a')\"")
    b = run_test("python -c \"print('b')\"")
    assert a.output_hash != b.output_hash


def test_output_hash_stable_for_same_output() -> None:
    a = run_test("python -c \"print('same')\"")
    b = run_test("python -c \"print('same')\"")
    assert a.output_hash == b.output_hash


def test_cwd_is_respected(tmp_path) -> None:
    (tmp_path / "marker.txt").write_text("here", encoding="utf-8")
    cmd = "python -c \"import os; assert os.path.exists('marker.txt')\""
    result = run_test(cmd, cwd=str(tmp_path))
    assert result.passed is True
