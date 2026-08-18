"""E23 Task 2: `.github/workflows/release.yaml` workflow-CONTRACT tests --
parse the YAML as structured data and assert properties about it. This
cannot execute a real GitHub Actions runner (no such thing available
locally), so it proves the workflow is *shaped* correctly: the right
triggers, the right job dependencies, every build checking out the
`resolve` job's own resolved SHA (never trusting the triggering ref
directly), all five artifact classes uploaded, the release job verifying
them, and the PyPI gate condition being exactly the repository-variable
check it must be. It does not and cannot prove the workflow succeeds when
GitHub Actions actually runs it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yaml"


def _load_workflow() -> dict[str, Any]:
    """YAML 1.1's implicit bool resolver turns a bare `on:` key into the
    Python bool `True` (a well-known PyYAML/GitHub-Actions gotcha) --
    normalize it back to the string `"on"` so this test reads naturally."""
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job.get("steps", [])


def _all_run_text(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _uses_steps(job: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    return [s for s in _steps(job) if str(s.get("uses", "")).startswith(prefix)]


def _checkout_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return _uses_steps(job, "actions/checkout")


# --------------------------------------------------------------------------
# Triggers
# --------------------------------------------------------------------------


def test_triggers_on_version_tag_push() -> None:
    doc = _load_workflow()
    tags = doc["on"]["push"]["tags"]
    assert "v*.*.*" in tags


def test_triggers_on_workflow_dispatch_with_required_tag_input() -> None:
    doc = _load_workflow()
    dispatch = doc["on"]["workflow_dispatch"]
    tag_input = dispatch["inputs"]["tag"]
    assert tag_input["required"] is True


# --------------------------------------------------------------------------
# resolve job
# --------------------------------------------------------------------------


def test_resolve_job_exists_and_invokes_resolve_tag() -> None:
    doc = _load_workflow()
    resolve = doc["jobs"]["resolve"]
    assert "resolve-tag" in _all_run_text(resolve)


def test_resolve_job_checks_out_the_default_branch_not_the_tag() -> None:
    """The resolve job must run the workflow's OWN code from `main` (so a
    repaired workflow file is what a recovery re-dispatch actually runs),
    not whatever ref triggered it."""
    doc = _load_workflow()
    resolve = doc["jobs"]["resolve"]
    checkouts = _checkout_steps(resolve)
    assert checkouts, "resolve job has no actions/checkout step"
    assert checkouts[0].get("with", {}).get("ref") == "main"


def test_resolve_job_exports_tag_and_sha_outputs() -> None:
    doc = _load_workflow()
    resolve = doc["jobs"]["resolve"]
    outputs = resolve.get("outputs", {})
    assert "tag" in outputs
    assert "sha" in outputs


def test_resolve_uses_dispatch_input_or_ref_name_depending_on_trigger() -> None:
    doc = _load_workflow()
    text = _all_run_text(doc["jobs"]["resolve"])
    assert "inputs.tag" in text
    assert "github.ref_name" in text
    assert "workflow_dispatch" in text


# --------------------------------------------------------------------------
# Every build job checks out the RESOLVED sha, not an implicit ref
# --------------------------------------------------------------------------


RESOLVED_SHA_EXPR = "${{ needs.resolve.outputs.sha }}"


def test_every_build_job_depends_on_resolve() -> None:
    doc = _load_workflow()
    for job_name in ("test", "build-source", "build-binaries", "release"):
        job = doc["jobs"][job_name]
        needs = job["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "resolve" in needs, f"{job_name} does not depend on resolve"


def test_every_build_job_checks_out_the_resolved_sha() -> None:
    doc = _load_workflow()
    for job_name in ("test", "build-source", "build-binaries", "release"):
        job = doc["jobs"][job_name]
        checkouts = _checkout_steps(job)
        assert checkouts, f"{job_name} has no actions/checkout step"
        ref = checkouts[0].get("with", {}).get("ref")
        assert ref == RESOLVED_SHA_EXPR, (
            f"{job_name}'s checkout ref is {ref!r}, expected the resolve job's own "
            f"resolved SHA {RESOLVED_SHA_EXPR!r}"
        )


# --------------------------------------------------------------------------
# Quality gate re-run against the candidate SHA
# --------------------------------------------------------------------------


def test_test_job_runs_the_same_required_quality_gates_as_ci() -> None:
    doc = _load_workflow()
    text = _all_run_text(doc["jobs"]["test"])
    assert "ruff check" in text
    assert "mypy belay" in text
    assert "pytest" in text
    assert "traceability.py --check" in text


# --------------------------------------------------------------------------
# Artifact production: source + three OS binaries
# --------------------------------------------------------------------------


def test_build_source_uploads_an_artifact() -> None:
    doc = _load_workflow()
    job = doc["jobs"]["build-source"]
    uploads = _uses_steps(job, "actions/upload-artifact")
    assert uploads, "build-source does not upload an artifact"


def test_build_binaries_matrix_covers_exactly_three_named_os_assets() -> None:
    doc = _load_workflow()
    job = doc["jobs"]["build-binaries"]
    include = job["strategy"]["matrix"]["include"]
    oses = {entry["os"] for entry in include}
    assert oses == {"ubuntu-latest", "macos-latest", "windows-latest"}
    asset_names = {entry["asset_name"] for entry in include}
    assert len(asset_names) == 3, "asset names must be distinct per OS"


def test_build_binaries_uploads_a_per_os_named_artifact() -> None:
    doc = _load_workflow()
    job = doc["jobs"]["build-binaries"]
    uploads = _uses_steps(job, "actions/upload-artifact")
    assert uploads, "build-binaries does not upload an artifact"
    assert uploads[0]["with"]["name"] == "${{ matrix.asset_name }}"


def test_build_binaries_runs_the_frozen_connect_smoke() -> None:
    """E23 Task 3: promoted to the required CI matrix -- also required
    here in the release workflow's own binary build."""
    doc = _load_workflow()
    text = _all_run_text(doc["jobs"]["build-binaries"])
    assert "smoke_connect.py" in text
    assert "--expect-frozen" in text


# --------------------------------------------------------------------------
# release job: download, verify, assemble
# --------------------------------------------------------------------------


def test_release_job_downloads_all_artifacts() -> None:
    doc = _load_workflow()
    job = doc["jobs"]["release"]
    downloads = _uses_steps(job, "actions/download-artifact")
    assert downloads, "release job does not download artifacts"
    # No `name:` -- downloads every artifact from every upload job above.
    assert "name" not in downloads[0].get("with", {})


def test_release_job_verifies_the_artifact_inventory() -> None:
    doc = _load_workflow()
    text = _all_run_text(doc["jobs"]["release"])
    assert "verify-artifacts" in text


def test_release_job_marks_prerelease_for_an_alpha_tag() -> None:
    doc = _load_workflow()
    text = _all_run_text(doc["jobs"]["release"])
    assert "is_prerelease" in text
    assert "--prerelease" in text


def test_release_job_never_uses_gh_release_delete_or_force_tag_commands() -> None:
    """The whole point of E23's recoverability model: the tag itself is
    NEVER moved/recreated by this workflow, only the release notes/assets
    attached to it."""
    doc = _load_workflow()
    text = _all_run_text(doc["jobs"]["release"])
    assert "git tag -f" not in text
    assert "git push --force" not in text
    assert "gh release delete" not in text


# --------------------------------------------------------------------------
# publish-pypi: exact repository-variable gate
# --------------------------------------------------------------------------


def test_publish_pypi_condition_is_exactly_the_repo_variable_gate() -> None:
    doc = _load_workflow()
    job = doc["jobs"]["publish-pypi"]
    assert job["if"] == "vars.PYPI_PUBLISH_ENABLED == 'true'"


def test_publish_pypi_depends_on_release() -> None:
    doc = _load_workflow()
    job = doc["jobs"]["publish-pypi"]
    needs = job["needs"]
    needs = [needs] if isinstance(needs, str) else needs
    assert "release" in needs


def test_publish_pypi_uses_trusted_publishing_no_token_secret() -> None:
    doc = _load_workflow()
    job = doc["jobs"]["publish-pypi"]
    text = _all_run_text(job)
    steps_text = str(job.get("steps", []))
    assert "password" not in text.lower()
    assert "PYPI_API_TOKEN" not in steps_text
    assert job.get("permissions", {}).get("id-token") == "write"
