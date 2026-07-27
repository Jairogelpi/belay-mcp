"""belay/ledger/test_evidence.py: real command/test execution and outcome recording.

Covers the fix for a second-round review finding: an arbitrary --cmd that
happens to pass must never be recorded as verifying a specific declared
test -- only run_declared_test (mode="test", command built mechanically
from RUNNERS) can produce that.
"""

from __future__ import annotations

import pytest
from belay.ledger.test_evidence import run_command, run_declared_test


def test_passing_command_recorded_as_passed() -> None:
    result = run_command("python -c \"print('ok')\"")
    assert result.passed is True
    assert result.exit_code == 0
    assert result.output_hash.startswith("sha256:")
    assert result.duration_ms >= 0
    assert result.mode == "command"
    assert result.test_ref is None


def test_failing_command_recorded_as_failed() -> None:
    result = run_command("python -c \"import sys; sys.exit(1)\"")
    assert result.passed is False
    assert result.exit_code == 1


def test_output_hash_differs_for_different_output() -> None:
    a = run_command("python -c \"print('a')\"")
    b = run_command("python -c \"print('b')\"")
    assert a.output_hash != b.output_hash


def test_output_hash_stable_for_same_output() -> None:
    a = run_command("python -c \"print('same')\"")
    b = run_command("python -c \"print('same')\"")
    assert a.output_hash == b.output_hash


def test_cwd_is_respected(tmp_path) -> None:
    (tmp_path / "marker.txt").write_text("here", encoding="utf-8")
    cmd = "python -c \"import os; assert os.path.exists('marker.txt')\""
    result = run_command(cmd, cwd=str(tmp_path))
    assert result.passed is True


def test_verified_by_recorded() -> None:
    result = run_command("python -c \"pass\"", verified_by="jairo")
    assert result.verified_by == "jairo"


def test_git_context_captured_in_a_real_repo(tmp_path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    result = run_command("python -c \"pass\"", cwd=str(tmp_path))
    assert result.git.head is not None
    assert result.git.tree_hash is not None
    assert result.git.dirty is False


def test_git_context_flags_dirty_worktree(tmp_path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    (tmp_path / "b.txt").write_text("uncommitted", encoding="utf-8")

    result = run_command("python -c \"pass\"", cwd=str(tmp_path))
    assert result.git.dirty is True


def test_run_declared_test_builds_command_from_ref_not_free_form() -> None:
    """The core fix: run_declared_test's command is built from `test_ref` via a
    fixed template -- there is no parameter through which a caller could
    substitute a different, easier-to-pass command under this ref."""
    result = run_declared_test("tests/definitely_does_not_exist.py::test_x", "pytest")
    assert result.mode == "test"
    assert result.test_ref == "tests/definitely_does_not_exist.py::test_x"
    assert result.cmd == "pytest tests/definitely_does_not_exist.py::test_x"
    assert result.passed is False  # the file doesn't exist -- pytest can't collect it


def test_run_declared_test_rejects_unknown_runner() -> None:
    with pytest.raises(ValueError, match="unknown runner"):
        run_declared_test("tests/x.py::test_y", "not_a_real_runner")
