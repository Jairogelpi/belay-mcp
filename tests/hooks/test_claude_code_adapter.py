"""belay/hooks/claude_code_adapter.py: normalize() and its git HEAD capture."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from belay.hooks.claude_code_adapter import _repo_identity, normalize


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


def test_repo_identity_returns_real_head_sha(tmp_path: Path) -> None:
    head = _init_repo(tmp_path)
    assert _repo_identity(str(tmp_path)) == head
    assert len(head) == 40  # a real git SHA-1, not a placeholder


def test_repo_identity_changes_after_a_new_commit(tmp_path: Path) -> None:
    first_head = _init_repo(tmp_path)
    (tmp_path / "g.txt").write_text("more\n", encoding="utf-8")
    subprocess.run(["git", "add", "g.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=tmp_path, check=True)

    second_head = _repo_identity(str(tmp_path))
    assert second_head != first_head


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
    assert event.repo_identity == head


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
    """P0 fix (original): no pinned-version conformance suite (spec §7.2)
    had run against a real Claude Code binary, so claiming T1
    unconditionally was an overclaim (TRUTH-004) -- it reported UNKNOWN
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
