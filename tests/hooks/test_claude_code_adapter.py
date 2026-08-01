"""belay/hooks/claude_code_adapter.py: normalize() and its git HEAD capture."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from belay.hooks.claude_code_adapter import _prestate_digest, _repo_identity, normalize


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "f.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def test_repo_identity_returns_real_head_sha_and_a_prestate_digest(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    assert _repo_identity(str(tmp_path)) == f"{head}:{_prestate_digest(str(tmp_path))}"
    assert len(head) == 40  # a real git SHA-1, not a placeholder


def test_repo_identity_changes_after_a_new_commit(tmp_path: Path) -> None:
    first_head = _init_repo(tmp_path)
    (tmp_path / "g.txt").write_text("more\n", encoding="utf-8")
    subprocess.run(["git", "add", "g.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=tmp_path, check=True)

    second_head = _repo_identity(str(tmp_path))
    assert second_head != first_head


def test_repo_identity_changes_after_an_uncommitted_change_to_a_tracked_file(
    tmp_path: Path,
) -> None:
    """R1.6: the concrete gap being closed -- an approval bound to
    `repo_identity` while the tree was clean must not silently cover a
    later call made after an uncommitted edit to the same repo."""
    _init_repo(tmp_path)
    clean_identity = _repo_identity(str(tmp_path))

    (tmp_path / "f.txt").write_text("modified, not committed\n", encoding="utf-8")

    dirty_identity = _repo_identity(str(tmp_path))
    assert dirty_identity != clean_identity


def test_two_different_uncommitted_edits_produce_two_different_identities(
    tmp_path: Path,
) -> None:
    """Post-R1.6 review finding: a bare dirty/clean boolean collapsed
    every dirty state into the same identity, so an approval granted
    against one uncommitted edit could in principle still resolve for a
    completely different one. The content-based prestate digest
    (`_prestate_digest`) fixes this: two distinct edits must hash
    differently, not just "any edit at all vs none"."""
    _init_repo(tmp_path)

    (tmp_path / "f.txt").write_text("edit A\n", encoding="utf-8")
    identity_a = _repo_identity(str(tmp_path))

    (tmp_path / "f.txt").write_text("edit B, completely different content\n", encoding="utf-8")
    identity_b = _repo_identity(str(tmp_path))

    assert identity_a != identity_b


def test_a_new_untracked_file_now_changes_the_identity(tmp_path: Path) -> None:
    """The known gap `_prestate_digest` narrows (does not fully close): a
    brand-new uncommitted file previously didn't change `repo_identity`
    at all (`git diff-index` only considers tracked content). Folding in
    `git status --porcelain`'s untracked-file listing means adding one
    now changes the identity -- not perfect (the new file's own content
    isn't hashed, only its path), but a real improvement over "not
    detected at all"."""
    _init_repo(tmp_path)
    before = _repo_identity(str(tmp_path))

    (tmp_path / "new_untracked.txt").write_text("brand new\n", encoding="utf-8")
    after = _repo_identity(str(tmp_path))

    assert after != before


def test_clean_tree_prestate_digest_is_the_same_across_independent_repos(
    tmp_path: Path,
) -> None:
    """The digest depends only on there being no diff from HEAD and no
    untracked files -- not on HEAD's value or the repo's committed
    content -- so two independently-initialized, both-clean repos share
    the same digest half of `repo_identity` (their full `repo_identity`
    still differs, via HEAD)."""
    repo_a, repo_b = tmp_path / "a", tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()
    _init_repo(repo_a)
    _init_repo(repo_b)

    assert _prestate_digest(str(repo_a)) == _prestate_digest(str(repo_b))


def test_repo_identity_is_clean_again_after_committing_the_change(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    clean_before = _repo_identity(str(tmp_path))
    (tmp_path / "f.txt").write_text("modified\n", encoding="utf-8")
    dirty = _repo_identity(str(tmp_path))
    assert dirty != clean_before

    subprocess.run(["git", "commit", "-aq", "-m", "second"], cwd=tmp_path, check=True)
    clean_after = _repo_identity(str(tmp_path))
    assert clean_after != dirty
    # Different HEAD now (a new commit), but genuinely clean again --
    # confirmed by the digest half matching the earlier clean state's,
    # not just "some new value that happens to differ from dirty".
    assert clean_after is not None
    assert clean_before is not None
    assert clean_after.rsplit(":", 1)[1] == clean_before.rsplit(":", 1)[1]


def test_repo_identity_is_none_outside_a_git_repo(tmp_path: Path) -> None:
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    assert _repo_identity(str(non_repo)) is None


def test_repo_identity_is_none_for_missing_cwd() -> None:
    assert _repo_identity(None) is None
    assert _repo_identity("") is None


def test_repo_identity_does_not_raise_for_nonexistent_directory() -> None:
    assert _repo_identity("/this/path/does/not/exist/anywhere") is None


def test_normalize_uses_real_repo_identity(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    raw = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_1",
        "cwd": str(tmp_path),
    }
    event = normalize(raw, installation_id="install1")
    assert event.repo_identity == f"{head}:{_prestate_digest(str(tmp_path))}"


@pytest.mark.parametrize(
    "tool_name,expected_surface",
    [
        ("Bash", "shell"),
        ("mcp__filesystem__read_file", "mcp"),
        ("Edit", "file"),
        ("Write", "file"),
        ("Read", "file"),
        ("NotebookEdit", "file"),
        ("SomethingElse", "other"),
    ],
)
def test_surface_classification(tool_name: str, expected_surface: str) -> None:
    raw = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {},
        "tool_use_id": "toolu_1",
    }
    event = normalize(raw, installation_id="install1")
    assert event.surface == expected_surface


def test_normalize_rejects_unrecognized_hook_event_name() -> None:
    with pytest.raises(ValueError, match="unrecognized"):
        normalize({"hook_event_name": "SomethingWeird"}, installation_id="i")


def test_trust_tier_is_t1_now_that_the_conformance_suite_actually_passed() -> None:
    """P0 fix (original): no pinned-version conformance suite (TRUTH-004,
    see docs/adr/0020-extended-requirement-catalog.md -- not a
    `docs/spec.md` §7.2 citation) had run against a real Claude Code
    binary, so claiming T1 unconditionally was an overclaim -- it
    reported UNKNOWN
    instead. E18.7 (tests/hooks/test_live_conformance.py) is that suite,
    it exists now and passed against the pinned real `claude` binary, so
    T1 here reflects real evidence, not a hardcoded assumption -- the
    same honesty bar applied in the other direction. If this ever needs
    to go back to UNKNOWN (an unpinned claude upgrade, a suite
    regression), that's a deliberate edit to
    `claude_code_adapter._VERIFIED_TRUST_TIER`, not this test's problem
    to guess at."""
    raw = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
        "tool_use_id": "toolu_1",
    }
    event = normalize(raw, installation_id="install1")
    assert event.trust_tier == "T1"


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Edit", {"file_path": "f.py", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "f.py", "content": "x"}),
        ("NotebookEdit", {"notebook_path": "n.ipynb"}),
        ("mcp__belay__wrap", {}),
    ],
)
def test_trust_tier_is_unknown_for_non_bash_surfaces_even_though_bash_is_t1(
    tool_name: str, tool_input: dict[str, object]
) -> None:
    """Regression: an earlier version applied `_VERIFIED_TRUST_TIER`
    (`"T1"`) to every Claude Code event regardless of `surface` -- a real
    overclaim, since `tests/hooks/test_live_conformance.py` (E18.7) only
    ever exercises the Bash surface. Edit/Write/NotebookEdit and native MCP
    calls must report `UNKNOWN`, same as an entirely unverified host, until
    each surface earns its own pinned-version conformance suite."""
    raw = {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "toolu_1",
    }
    event = normalize(raw, installation_id="install1")
    assert event.trust_tier == "UNKNOWN"


class TestPostToolUseResultExtraction:
    """The field name Claude Code actually uses for a PostToolUse result
    (`tool_response` vs `tool_result`) could not be pinned down with full
    confidence from available docs (two fetches gave different answers) --
    these tests lock in the DEFENSIVE behavior actually implemented (try
    both, never fabricate a value for a field that isn't there), not a
    single assumed-correct schema."""

    def _post_raw(self, **result_field: object) -> dict[str, object]:
        return {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_post_1",
            **result_field,
        }

    def test_tool_response_field_name_is_recognized(self) -> None:
        raw = self._post_raw(tool_response={"exit_code": 0, "stdout": "ok", "stderr": ""})
        event = normalize(raw, installation_id="i")
        assert event.exit_code == 0
        assert event.result_status == "success"
        assert event.output_digest is not None

    def test_tool_result_field_name_is_also_recognized(self) -> None:
        raw = self._post_raw(tool_result={"exit_code": 1, "stdout": "", "stderr": "boom"})
        event = normalize(raw, installation_id="i")
        assert event.exit_code == 1
        assert event.result_status == "failure"

    def test_camelcase_exit_code_key_is_recognized(self) -> None:
        raw = self._post_raw(tool_response={"exitCode": 0})
        event = normalize(raw, installation_id="i")
        assert event.exit_code == 0

    def test_missing_result_field_entirely_leaves_everything_none_not_guessed(self) -> None:
        raw = self._post_raw()
        event = normalize(raw, installation_id="i")
        assert event.exit_code is None
        assert event.result_status is None
        assert event.output_digest is None
        assert event.truncated is None

    def test_non_dict_result_field_is_ignored_not_crashed_on(self) -> None:
        raw = self._post_raw(tool_response="not-a-dict")
        event = normalize(raw, installation_id="i")
        assert event.exit_code is None

    def test_truncated_flag_is_extracted_when_present(self) -> None:
        raw = self._post_raw(tool_response={"exit_code": 0, "truncated": True})
        event = normalize(raw, installation_id="i")
        assert event.truncated is True

    def test_output_digest_differs_for_different_output(self) -> None:
        a = normalize(self._post_raw(tool_response={"stdout": "aaa"}), installation_id="i")
        b = normalize(self._post_raw(tool_response={"stdout": "bbb"}), installation_id="i")
        assert a.output_digest != b.output_digest

    def test_pre_phase_never_populates_result_fields_even_if_present(self) -> None:
        """A PreToolUse payload has no result to extract from -- and even if
        some future/odd payload happened to carry a `tool_response`-shaped
        key on a PreToolUse event, it must not be misread as a result."""
        raw = {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "toolu_1",
            "tool_response": {"exit_code": 0},
        }
        event = normalize(raw, installation_id="i")
        assert event.exit_code is None
        assert event.result_status is None
