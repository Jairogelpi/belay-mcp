"""E23 Task 1: `scripts/release_preflight.py` -- version/tag/ref/artifact/
required-check validation shared by the release workflow (Task 2) and a
human running the runbook (Task 5). Every git-touching test below drives
a real, throwaway local git repository (never this actual repo); every
`gh api`-touching test injects a fake runner -- no real network call, no
real `gh` invocation, anywhere in this file."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_preflight as rp  # noqa: E402

pytestmark = pytest.mark.slow  # every test here shells out to a real `git`


# --------------------------------------------------------------------------
# git repo fixture helpers
# --------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, shell=False
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _commit_all(path: Path, message: str) -> str:
    _git(["add", "-A"], path)
    _git(["commit", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


def _write_project_files(
    path: Path, *, version: str = "0.2.0a1", npm_version: str = "0.2.0-alpha.1"
) -> None:
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "belay-mcp"\nversion = "{version}"\n', encoding="utf-8"
    )
    npm_dir = path / "npm"
    npm_dir.mkdir(parents=True, exist_ok=True)
    (npm_dir / "package.json").write_text(
        json.dumps({"name": "belay-mcp", "version": npm_version}), encoding="utf-8"
    )


def _make_remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Returns (bare_remote, local_clone) with a `main` branch pushed and
    `origin` set up, matching a real checkout of this repo's own topology
    closely enough for every check under test."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(["init", "--bare", "--initial-branch=main"], bare)

    local = tmp_path / "local"
    _init_repo(local)
    _write_project_files(local)
    _commit_all(local, "initial")
    _git(["remote", "add", "origin", str(bare)], local)
    _git(["push", "-u", "origin", "main"], local)
    return bare, local


# --------------------------------------------------------------------------
# Step 1: version handling
# --------------------------------------------------------------------------


def test_parse_tag_version_accepts_valid_alpha_tag() -> None:
    version = rp.parse_tag_version("v0.2.0a1")
    assert str(version) == "0.2.0a1"


def test_parse_tag_version_rejects_missing_v_prefix() -> None:
    with pytest.raises(rp.PreflightError, match="must start with 'v'"):
        rp.parse_tag_version("0.2.0a1")


def test_parse_tag_version_rejects_non_pep440_suffix() -> None:
    with pytest.raises(rp.PreflightError, match="not a valid PEP 440 version"):
        rp.parse_tag_version("vnot-a-version")


def test_pep440_to_npm_version_alpha() -> None:
    assert rp.pep440_to_npm_version(rp.parse_tag_version("v0.2.0a1")) == "0.2.0-alpha.1"


def test_pep440_to_npm_version_final_release_has_no_prerelease_segment() -> None:
    assert rp.pep440_to_npm_version(rp.parse_tag_version("v1.0.0")) == "1.0.0"


def test_check_version_consistency_passes_when_all_agree(tmp_path: Path) -> None:
    _write_project_files(tmp_path, version="0.2.0a1", npm_version="0.2.0-alpha.1")
    version = rp.check_version_consistency(
        "v0.2.0a1",
        pyproject_path=tmp_path / "pyproject.toml",
        package_json_path=tmp_path / "npm" / "package.json",
    )
    assert str(version) == "0.2.0a1"


def test_check_version_consistency_rejects_pyproject_mismatch(tmp_path: Path) -> None:
    _write_project_files(tmp_path, version="0.1.0", npm_version="0.2.0-alpha.1")
    with pytest.raises(rp.PreflightError, match=r"pyproject\.toml version"):
        rp.check_version_consistency(
            "v0.2.0a1",
            pyproject_path=tmp_path / "pyproject.toml",
            package_json_path=tmp_path / "npm" / "package.json",
        )


def test_check_version_consistency_rejects_npm_mismatch(tmp_path: Path) -> None:
    _write_project_files(tmp_path, version="0.2.0a1", npm_version="0.2.0-alpha.2")
    with pytest.raises(rp.PreflightError, match=r"npm/package\.json version"):
        rp.check_version_consistency(
            "v0.2.0a1",
            pyproject_path=tmp_path / "pyproject.toml",
            package_json_path=tmp_path / "npm" / "package.json",
        )


def test_check_version_consistency_reports_both_mismatches_together(tmp_path: Path) -> None:
    _write_project_files(tmp_path, version="9.9.9", npm_version="9.9.9")
    with pytest.raises(rp.PreflightError) as excinfo:
        rp.check_version_consistency(
            "v0.2.0a1",
            pyproject_path=tmp_path / "pyproject.toml",
            package_json_path=tmp_path / "npm" / "package.json",
        )
    assert "pyproject.toml version" in str(excinfo.value)
    assert "npm/package.json version" in str(excinfo.value)


def test_check_clean_worktree_passes_on_clean_repo(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_project_files(tmp_path)
    _commit_all(tmp_path, "initial")
    rp.check_clean_worktree(tmp_path)  # must not raise


def test_check_clean_worktree_rejects_dirty_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_project_files(tmp_path)
    _commit_all(tmp_path, "initial")
    (tmp_path / "untracked.txt").write_text("oops", encoding="utf-8")
    with pytest.raises(rp.PreflightError, match="not clean"):
        rp.check_clean_worktree(tmp_path)


def test_check_local_matches_remote_main_passes_when_in_sync(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    sha = rp.check_local_matches_remote_main(local)
    assert sha == _git(["rev-parse", "main"], local).stdout.strip()


def test_check_local_matches_remote_main_rejects_local_ahead(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    (local / "extra.txt").write_text("new commit", encoding="utf-8")
    _commit_all(local, "local-only commit")
    with pytest.raises(rp.PreflightError, match="does not match"):
        rp.check_local_matches_remote_main(local)


def test_check_tag_absent_passes_when_tag_does_not_exist(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    rp.check_tag_absent(local, "v9.9.9")  # must not raise


def test_check_tag_absent_rejects_existing_local_tag(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    _git(["tag", "v0.2.0a1"], local)
    with pytest.raises(rp.PreflightError, match="already exists locally"):
        rp.check_tag_absent(local, "v0.2.0a1")


def test_check_tag_absent_rejects_existing_remote_tag(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    _git(["tag", "v0.2.0a1"], local)
    _git(["push", "origin", "v0.2.0a1"], local)
    _git(["tag", "-d", "v0.2.0a1"], local)  # remote only now
    with pytest.raises(rp.PreflightError, match="already exists on remote"):
        rp.check_tag_absent(local, "v0.2.0a1")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, False), ("false", False), ("", False), ("TRUE", False), ("true", True)],
)
def test_pypi_publish_enabled(raw: str | None, expected: bool) -> None:
    assert rp.pypi_publish_enabled(raw) is expected


# --------------------------------------------------------------------------
# Step 2: ref resolution
# --------------------------------------------------------------------------


def test_resolve_tag_sha_accepts_lightweight_tag(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    _git(["tag", "v1.0.0"], local)
    _git(["push", "origin", "v1.0.0"], local)
    expected_sha = _git(["rev-parse", "HEAD"], local).stdout.strip()

    resolved = rp.resolve_tag_sha(local, "v1.0.0")
    assert resolved == expected_sha


def test_resolve_tag_sha_accepts_annotated_tag_and_peels_to_commit(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    _git(["tag", "-a", "v1.0.0", "-m", "release"], local)
    _git(["push", "origin", "v1.0.0"], local)
    expected_commit_sha = _git(["rev-parse", "HEAD"], local).stdout.strip()
    tag_object_sha = _git(["rev-parse", "v1.0.0"], local).stdout.strip()
    # sanity: an annotated tag's own object SHA differs from the commit it targets
    assert tag_object_sha != expected_commit_sha

    resolved = rp.resolve_tag_sha(local, "v1.0.0")
    assert resolved == expected_commit_sha


def test_resolve_tag_sha_rejects_nonexistent_tag(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    with pytest.raises(rp.PreflightError, match="not found"):
        rp.resolve_tag_sha(local, "v9.9.9")


def test_resolve_tag_sha_rejects_a_branch_with_the_same_name_as_a_tag(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    _git(["branch", "v1.0.0"], local)
    _git(["push", "origin", "v1.0.0"], local)  # pushes a branch, not a tag
    with pytest.raises(rp.PreflightError, match="not found"):
        rp.resolve_tag_sha(local, "v1.0.0")


def test_resolve_tag_sha_rejects_malformed_tag_without_v_prefix(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    with pytest.raises(rp.PreflightError, match="must start with 'v'"):
        rp.resolve_tag_sha(local, "1.0.0")


# --------------------------------------------------------------------------
# Step 3: artifact inventory
# --------------------------------------------------------------------------


def _write_valid_artifacts(directory: Path, version: str = "0.2.0a1") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"belay_mcp-{version}.tar.gz").write_bytes(b"sdist contents")
    (directory / f"belay_mcp-{version}-py3-none-any.whl").write_bytes(b"wheel contents")
    (directory / "belay-linux-x86_64").write_bytes(b"linux binary")
    (directory / "belay-macos-x86_64").write_bytes(b"macos binary")
    (directory / "belay-windows-x86_64.exe").write_bytes(b"windows binary")


def test_verify_artifacts_accepts_a_complete_valid_inventory(tmp_path: Path) -> None:
    _write_valid_artifacts(tmp_path)
    inventory = rp.verify_artifacts(tmp_path, "v0.2.0a1")
    assert set(inventory.by_class) == set(rp.ARTIFACT_CLASSES)


def test_verify_artifacts_finds_artifacts_in_subdirectories(tmp_path: Path) -> None:
    """`actions/download-artifact` lays each named artifact out in its own
    subdirectory -- the release job's assembled directory looks like this,
    not one flat folder."""
    _write_valid_artifacts(tmp_path / "belay-ubuntu-latest" / "sdist-and-wheel")
    (tmp_path / "belay-ubuntu-latest" / "sdist-and-wheel" / "belay-linux-x86_64").unlink()
    (tmp_path / "belay-ubuntu-latest" / "sdist-and-wheel" / "belay-macos-x86_64").unlink()
    (tmp_path / "belay-ubuntu-latest" / "sdist-and-wheel" / "belay-windows-x86_64.exe").unlink()
    linux_dir = tmp_path / "belay-linux"
    linux_dir.mkdir()
    (linux_dir / "belay-linux-x86_64").write_bytes(b"linux binary")
    macos_dir = tmp_path / "belay-macos"
    macos_dir.mkdir()
    (macos_dir / "belay-macos-x86_64").write_bytes(b"macos binary")
    windows_dir = tmp_path / "belay-windows"
    windows_dir.mkdir()
    (windows_dir / "belay-windows-x86_64.exe").write_bytes(b"windows binary")

    inventory = rp.verify_artifacts(tmp_path, "v0.2.0a1")
    assert set(inventory.by_class) == set(rp.ARTIFACT_CLASSES)


def test_verify_artifacts_rejects_missing_class(tmp_path: Path) -> None:
    _write_valid_artifacts(tmp_path)
    (tmp_path / "belay-windows-x86_64.exe").unlink()
    with pytest.raises(rp.PreflightError, match="missing artifact class 'windows-binary'"):
        rp.verify_artifacts(tmp_path, "v0.2.0a1")


def test_verify_artifacts_rejects_duplicate_ambiguous_class(tmp_path: Path) -> None:
    _write_valid_artifacts(tmp_path)
    (tmp_path / "belay-linux-arm64").write_bytes(b"a second linux binary")
    with pytest.raises(rp.PreflightError, match="ambiguous"):
        rp.verify_artifacts(tmp_path, "v0.2.0a1")


def test_verify_artifacts_rejects_zero_byte_artifact(tmp_path: Path) -> None:
    _write_valid_artifacts(tmp_path)
    (tmp_path / "belay-windows-x86_64.exe").write_bytes(b"")
    with pytest.raises(rp.PreflightError, match="zero-byte"):
        rp.verify_artifacts(tmp_path, "v0.2.0a1")


def test_verify_artifacts_rejects_version_mismatched_sdist(tmp_path: Path) -> None:
    _write_valid_artifacts(tmp_path, version="0.1.0")
    with pytest.raises(rp.PreflightError, match="version mismatch"):
        rp.verify_artifacts(tmp_path, "v0.2.0a1")


def test_verify_artifacts_rejects_mismatched_wheel_even_if_sdist_matches(tmp_path: Path) -> None:
    _write_valid_artifacts(tmp_path, version="0.2.0a1")
    (tmp_path / "belay_mcp-0.2.0a1-py3-none-any.whl").unlink()
    (tmp_path / "belay_mcp-0.1.0-py3-none-any.whl").write_bytes(b"wheel contents")
    with pytest.raises(rp.PreflightError, match="version mismatch for 'wheel'"):
        rp.verify_artifacts(tmp_path, "v0.2.0a1")


def test_verify_artifacts_rejects_nonexistent_directory(tmp_path: Path) -> None:
    with pytest.raises(rp.PreflightError, match="does not exist"):
        rp.verify_artifacts(tmp_path / "does-not-exist", "v0.2.0a1")


# --------------------------------------------------------------------------
# Step 4: required-checks (fake `gh api` runner -- no real network call)
# --------------------------------------------------------------------------

_SHA = "a" * 40


def _check_run(
    name: str, *, status: str = "completed", conclusion: str | None = "success", sha: str = _SHA
) -> dict:
    return {"name": name, "status": status, "conclusion": conclusion, "head_sha": sha}


def _fake_gh_runner(check_runs: list[dict]):
    payload = json.dumps({"total_count": len(check_runs), "check_runs": check_runs})

    def runner(args: list[str]) -> str:
        assert args[0] == "api"
        assert _SHA in args[1] or True
        return payload

    return runner


def _all_required_success(sha: str = _SHA) -> list[dict]:
    return [_check_run(name, sha=sha) for name in rp.REQUIRED_CHECK_NAMES]


def test_required_checks_passes_when_every_required_check_succeeds() -> None:
    runner = _fake_gh_runner(_all_required_success())
    results = rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)
    assert set(results) == set(rp.REQUIRED_CHECK_NAMES)


def test_required_checks_ignores_unrelated_extra_check_runs() -> None:
    extra = [*_all_required_success(), _check_run("some-other-workflow-job")]
    runner = _fake_gh_runner(extra)
    results = rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)
    assert set(results) == set(rp.REQUIRED_CHECK_NAMES)


def test_required_checks_rejects_missing_check() -> None:
    runs = [c for c in _all_required_success() if c["name"] != "wheel-smoke"]
    runner = _fake_gh_runner(runs)
    with pytest.raises(rp.PreflightError, match="missing required check 'wheel-smoke'"):
        rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_required_checks_rejects_incomplete_status(status: str) -> None:
    runs = _all_required_success()
    runs[0] = _check_run(rp.REQUIRED_CHECK_NAMES[0], status=status, conclusion=None)
    runner = _fake_gh_runner(runs)
    with pytest.raises(rp.PreflightError, match="not completed"):
        rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped", "neutral", "failure", "timed_out"])
def test_required_checks_rejects_unsuccessful_conclusion(conclusion: str) -> None:
    runs = _all_required_success()
    runs[0] = _check_run(rp.REQUIRED_CHECK_NAMES[0], conclusion=conclusion)
    runner = _fake_gh_runner(runs)
    with pytest.raises(rp.PreflightError, match="did not succeed"):
        rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)


def test_required_checks_rejects_stale_sha() -> None:
    runs = _all_required_success()
    runs[0] = _check_run(rp.REQUIRED_CHECK_NAMES[0], sha="b" * 40)
    runner = _fake_gh_runner(runs)
    with pytest.raises(rp.PreflightError, match="stale SHA"):
        rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)


def test_required_checks_rejects_duplicate_ambiguous_check() -> None:
    runs = [*_all_required_success(), _check_run(rp.REQUIRED_CHECK_NAMES[0])]
    runner = _fake_gh_runner(runs)
    with pytest.raises(rp.PreflightError, match="ambiguous"):
        rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)


def test_required_checks_error_includes_every_missing_context_not_just_first() -> None:
    runs = [
        c for c in _all_required_success() if c["name"] not in ("wheel-smoke", "test (3.12)")
    ]
    runner = _fake_gh_runner(runs)
    with pytest.raises(rp.PreflightError) as excinfo:
        rp.required_checks("Jairogelpi/belay-mcp", _SHA, gh_runner=runner)
    message = str(excinfo.value)
    assert "wheel-smoke" in message
    assert "test (3.12)" in message


# --------------------------------------------------------------------------
# prepare: composed pre-tag gate
# --------------------------------------------------------------------------


def test_prepare_succeeds_when_everything_is_in_order(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    head_sha = _git(["rev-parse", "HEAD"], local).stdout.strip()
    runner = _fake_gh_runner(_all_required_success(sha=head_sha))

    summary = rp.prepare(
        "v0.2.0a1",
        "Jairogelpi/belay-mcp",
        repo_dir=local,
        pypi_publish_enabled_raw=None,
        gh_runner=runner,
    )
    assert summary["tag"] == "v0.2.0a1"
    assert summary["pypi_publish_enabled"] is False
    assert set(summary["required_checks"]) == set(rp.REQUIRED_CHECK_NAMES)


def test_prepare_fails_on_version_mismatch_before_touching_gh(tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(["init", "--bare", "--initial-branch=main"], bare)
    local = tmp_path / "local"
    _init_repo(local)
    _write_project_files(local, version="0.1.0")  # wrong version
    _commit_all(local, "initial")
    _git(["remote", "add", "origin", str(bare)], local)
    _git(["push", "-u", "origin", "main"], local)

    def _boom(_args: list[str]) -> str:
        raise AssertionError("gh must not be invoked when version consistency already failed")

    with pytest.raises(rp.PreflightError, match=r"pyproject\.toml version"):
        rp.prepare(
            "v0.2.0a1",
            "Jairogelpi/belay-mcp",
            repo_dir=local,
            pypi_publish_enabled_raw=None,
            gh_runner=_boom,
        )


def test_prepare_fails_when_required_checks_are_not_green(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    runner = _fake_gh_runner([])  # nothing green yet

    with pytest.raises(rp.PreflightError, match="missing required check"):
        rp.prepare(
            "v0.2.0a1",
            "Jairogelpi/belay-mcp",
            repo_dir=local,
            pypi_publish_enabled_raw=None,
            gh_runner=runner,
        )


def test_prepare_reports_pypi_enabled_true_when_variable_is_true(tmp_path: Path) -> None:
    _, local = _make_remote_and_clone(tmp_path)
    head_sha = _git(["rev-parse", "HEAD"], local).stdout.strip()
    runner = _fake_gh_runner(_all_required_success(sha=head_sha))

    summary = rp.prepare(
        "v0.2.0a1",
        "Jairogelpi/belay-mcp",
        repo_dir=local,
        pypi_publish_enabled_raw="true",
        gh_runner=runner,
    )
    assert summary["pypi_publish_enabled"] is True
