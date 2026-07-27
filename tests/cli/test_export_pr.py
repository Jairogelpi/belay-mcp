"""belay/cli/export_pr.py: file-change extraction and git safety guards.

Covers two real bugs a review caught before this landed (2026-07-27):
a read-only step misclassified as a delete (no `content` in args !=
`effects` says `delete`), and unconfined/unchecked git operations.
"""

from __future__ import annotations

import subprocess

import pytest
from belay.cli.export_pr import (
    ExportPrError,
    _confine_to_repo,
    apply_changes,
    build_proof_body,
    extract_file_changes,
)
from belay.ledger.store import LedgerStore


def _committed_step(
    ledger: LedgerStore, sid: str, step_seq: int, tool: str, args: dict, effects: list
) -> None:
    ledger.append(
        sid, "plan_created", {"tool": tool, "args": args, "effects": effects}, step_seq=step_seq
    )
    ledger.append(sid, "step_committed", {"tool": tool}, step_seq=step_seq)


def test_write_effect_extracted_as_change_with_content() -> None:
    ledger = LedgerStore()
    sid = "s_test"
    _committed_step(
        ledger,
        sid,
        1,
        "fs.write_file",
        {"path": "a.py", "content": "v1"},
        [{"type": "update", "resource": "fs.file"}],
    )
    changes = extract_file_changes(ledger.read(sid))
    assert len(changes) == 1
    assert changes[0].path == "a.py"
    assert changes[0].after == "v1"


def test_delete_effect_extracted_with_after_none() -> None:
    ledger = LedgerStore()
    sid = "s_test"
    _committed_step(
        ledger, sid, 1, "fs.delete_file", {"path": "a.py"},
        [{"type": "delete", "resource": "fs.file"}],
    )
    changes = extract_file_changes(ledger.read(sid))
    assert len(changes) == 1
    assert changes[0].after is None


def test_read_only_step_is_not_a_file_change() -> None:
    """The real bug found in review: fs.read_file(path=...) has no `content` in
    its args, same shape as a delete would have -- must NOT be classified as
    deleting that path just because `content` is absent."""
    ledger = LedgerStore()
    sid = "s_test"
    _committed_step(
        ledger, sid, 1, "fs.read_file", {"path": "README.md"},
        [{"type": "read", "resource": "fs.file"}],
    )
    changes = extract_file_changes(ledger.read(sid))
    assert changes == []


def test_step_with_no_path_arg_ignored() -> None:
    ledger = LedgerStore()
    sid = "s_test"
    _committed_step(
        ledger, sid, 1, "crm.bulk_delete", {"before_year": 2020},
        [{"type": "delete", "resource": "crm.record"}],
    )
    changes = extract_file_changes(ledger.read(sid))
    assert changes == []


def test_confine_to_repo_allows_path_inside(tmp_path) -> None:
    target = _confine_to_repo(tmp_path, "src/a.py")
    assert target == (tmp_path / "src" / "a.py").resolve()


def test_confine_to_repo_refuses_traversal_outside(tmp_path) -> None:
    with pytest.raises(ExportPrError, match="outside repo"):
        _confine_to_repo(tmp_path, "../../etc/passwd")


def test_confine_to_repo_refuses_absolute_path_outside(tmp_path) -> None:
    with pytest.raises(ExportPrError, match="outside repo"):
        _confine_to_repo(tmp_path, "/etc/passwd")


def test_checkout_branch_refuses_dirty_worktree(tmp_path) -> None:
    from belay.cli.export_pr import checkout_branch

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "committed.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True)
    (tmp_path / "committed.txt").write_text("dirty, never committed", encoding="utf-8")

    with pytest.raises(ExportPrError, match="uncommitted changes"):
        checkout_branch(tmp_path, "belay/test", "main")


def test_apply_changes_writes_onto_checked_out_branch_not_prior_one(tmp_path) -> None:
    """The real bug found in review: applying changes before checking out the PR
    branch would land them on whatever branch was already checked out (main)
    instead of the new one. checkout_branch must run first."""
    from belay.cli.export_pr import FileChange, checkout_branch

    remote = tmp_path / "remote"
    work = tmp_path / "work"
    remote.mkdir()
    work.mkdir()
    subprocess.run(["git", "init", "--bare", "-q"], cwd=remote, check=True)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=work, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=work, check=True)
    (work / "a.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=work, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=work, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main", "-q"], cwd=work, check=True)

    checkout_branch(work, "belay/s_test", "main")
    current_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=work, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert current_branch == "belay/s_test"

    change = FileChange(step_seq=1, tool="fs.write_file", path="a.txt", before=None, after="v2")
    apply_changes(work, [change])
    assert (work / "a.txt").read_text(encoding="utf-8") == "v2"


def test_build_proof_body_marks_verified_intent() -> None:
    body = build_proof_body(
        "s_1", [], [], "", "Add timezone", None, None, intent_verified=True
    )
    assert "_verified: this contract's hash matches" in body
    assert "UNVERIFIED" not in body


def test_build_proof_body_flags_unverified_intent() -> None:
    body = build_proof_body(
        "s_1", [], [], "", "Add timezone", None, None, intent_verified=False
    )
    assert "UNVERIFIED" in body
    assert "does NOT match" in body


def test_build_proof_body_no_intent_says_not_declared() -> None:
    body = build_proof_body("s_1", [], [], "", None, None, None, intent_verified=None)
    assert "not declared" in body
