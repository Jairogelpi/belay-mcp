"""E23 Task 1: preflight validation for a Belay GitHub prerelease.

Everything a human (or the release workflow, Task 2) needs to know
*before* an immutable tag is created or a release is assembled, in one
testable place shared by both paths -- not duplicated ad hoc in YAML and
in a human's head.

Four subcommands, each independently useful and independently testable:

    resolve-tag      Resolve an existing `v*` tag on a remote to its exact
                      target commit SHA (annotated tags are peeled).
    required-checks   Verify every one of E23's nine required CI check-run
                      names is present, on the exact candidate SHA, and
                      completed successfully -- no duplicates, no stale
                      SHA, no queued/in-progress/cancelled/skipped/neutral
                      result silently accepted.
    verify-artifacts  Verify a directory contains exactly the five release
                      artifact classes (sdist, wheel, Linux/macOS/Windows
                      binaries) belay-mcp ships, with no missing,
                      duplicate/ambiguous, zero-byte, or version-mismatched
                      files.
    prepare           The full pre-tag gate: tag format, tag/version
                      consistency (PEP 440 <-> npm semver), clean
                      worktree, local `main` == `origin/main`, tag absent
                      both locally and on the remote, the PyPI publish gate
                      state, and (by resolving `origin/main`'s own SHA and
                      calling `required-checks` against it) every required
                      check green on that exact candidate commit.

Every subcommand fails closed: on success it prints one JSON object to
stdout and exits 0; on failure it prints a human-readable error (every
distinct problem found, not just the first) to stderr and exits 1. No
subcommand mutates repository state -- this script only ever reads.

`required-checks` shells out to `gh api` through an injectable runner
(`--gh-bin`, or programmatically the `gh_runner` parameter) so the test
suite can supply a fake instead of ever making a real network call; every
other subcommand shells out to the local `git` binary against a real (or
in tests, a real temporary) repository -- there is nothing to fake there,
git's own behavior is exactly what's under test.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[1]

# The exact nine required check-run names E23 promotes to the release
# gate (plan-v2/E23 Task 1 Step 4): the two Python-version unit-test jobs,
# the wheel-only smoke, all three OS clean-room jobs, and all three OS
# binary builds. Order here is cosmetic (error/output ordering only) --
# matching is by name, not position.
REQUIRED_CHECK_NAMES: tuple[str, ...] = (
    "test (3.12)",
    "test (3.13)",
    "wheel-smoke",
    "cross-platform-clean-room (ubuntu-latest)",
    "cross-platform-clean-room (macos-latest)",
    "cross-platform-clean-room (windows-latest)",
    "build-binaries (ubuntu-latest)",
    "build-binaries (macos-latest)",
    "build-binaries (windows-latest)",
)

# PEP 440 pre-release segment letter -> npm/semver prerelease word. Belay
# has only ever used "a" (alpha) so far; "b"/"rc" are included because
# `packaging.version.Version` accepts them and a future tag legitimately
# could use one -- silently mishandling that later would be worse than
# spelling it out now.
_PEP440_TO_NPM_PRERELEASE = {"a": "alpha", "b": "beta", "rc": "rc"}


class PreflightError(RuntimeError):
    """One or more release-readiness checks failed. `str(exc)` is a
    complete, human-readable report of every problem found -- callers
    should not need to catch and re-wrap it to get a useful message."""


# --------------------------------------------------------------------------
# Version handling: tag <-> pyproject (PEP 440) <-> npm (semver)
# --------------------------------------------------------------------------


def parse_tag_version(tag: str) -> Version:
    """Validate `tag` is `v` + a PEP 440 version and return the parsed
    `Version`. Raises `PreflightError` on anything else -- no bare `v`,
    no missing `v` prefix, no non-PEP-440 suffix."""
    if not tag.startswith("v") or len(tag) < 2:
        raise PreflightError(f"malformed tag {tag!r}: must start with 'v' followed by a version")
    raw = tag[1:]
    try:
        return Version(raw)
    except InvalidVersion as exc:
        raise PreflightError(
            f"malformed tag {tag!r}: {raw!r} is not a valid PEP 440 version"
        ) from exc


def pep440_to_npm_version(version: Version) -> str:
    """`0.2.0a1` -> `0.2.0-alpha.1`; `1.0.0` -> `1.0.0` (no prerelease
    segment). Belay's npm wrapper (`npm/package.json`) uses semver, which
    has no native `aN`/`bN`/`rcN` shorthand -- it spells the word out."""
    core = f"{version.major}.{version.minor}.{version.micro}"
    if version.pre is None:
        return core
    letter, number = version.pre
    word = _PEP440_TO_NPM_PRERELEASE.get(letter)
    if word is None:
        raise PreflightError(f"unsupported PEP 440 pre-release segment {letter!r} in {version}")
    return f"{core}-{word}.{number}"


def read_pyproject_version(pyproject_path: Path) -> str:
    text = pyproject_path.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not match:
        raise PreflightError(f"could not find a `version = \"...\"` line in {pyproject_path}")
    return match.group(1)


def read_npm_version(package_json_path: Path) -> str:
    doc = json.loads(package_json_path.read_text(encoding="utf-8"))
    version = doc.get("version")
    if not isinstance(version, str) or not version:
        raise PreflightError(f"could not find a string \"version\" field in {package_json_path}")
    return version


def check_version_consistency(
    tag: str, *, pyproject_path: Path, package_json_path: Path
) -> Version:
    """Raises `PreflightError` (listing every mismatch found, not just the
    first) unless `tag`, `pyproject.toml`'s `version`, and
    `npm/package.json`'s `version` all name the exact same release."""
    tag_version = parse_tag_version(tag)
    expected_pep440 = str(tag_version)
    expected_npm = pep440_to_npm_version(tag_version)

    problems: list[str] = []

    pyproject_version = read_pyproject_version(pyproject_path)
    if pyproject_version != expected_pep440:
        problems.append(
            f"pyproject.toml version {pyproject_version!r} does not match tag-derived "
            f"PEP 440 version {expected_pep440!r} (from tag {tag!r})"
        )

    npm_version = read_npm_version(package_json_path)
    if npm_version != expected_npm:
        problems.append(
            f"npm/package.json version {npm_version!r} does not match tag-derived npm "
            f"version {expected_npm!r} (from tag {tag!r})"
        )

    if problems:
        raise PreflightError("version mismatch:\n" + "\n".join(f"  - {p}" for p in problems))
    return tag_version


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


def _run_git(args: Sequence[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, shell=False
    )
    if result.returncode != 0:
        raise PreflightError(
            f"git {' '.join(args)} (in {cwd}) failed rc={result.returncode}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def check_clean_worktree(repo_dir: Path) -> None:
    status = _run_git(["status", "--porcelain"], cwd=repo_dir)
    if status.strip():
        raise PreflightError(
            "worktree is not clean -- `git status --porcelain` reported:\n" + status
        )


def check_local_matches_remote_main(
    repo_dir: Path, *, remote: str = "origin", branch: str = "main"
) -> str:
    """Raises unless local `branch` and `remote/branch` point at the exact
    same commit. Returns that commit SHA on success."""
    local_sha = _run_git(["rev-parse", branch], cwd=repo_dir).strip()
    remote_sha = _run_git(["rev-parse", f"{remote}/{branch}"], cwd=repo_dir).strip()
    if local_sha != remote_sha:
        raise PreflightError(
            f"local {branch!r} ({local_sha}) does not match {remote}/{branch} ({remote_sha}) -- "
            f"fetch/pull/push before release"
        )
    return local_sha


def check_tag_absent(repo_dir: Path, tag: str, *, remote: str = "origin") -> None:
    """Raises if `tag` already exists locally or on `remote` -- tags in
    this repo's release process are created exactly once and never
    moved (docs/release-runbook.md)."""
    local = _run_git(["tag", "--list", tag], cwd=repo_dir).strip()
    if local:
        raise PreflightError(f"tag {tag!r} already exists locally -- tags are never recreated")
    remote_out = _run_git(["ls-remote", "--tags", remote, f"refs/tags/{tag}"], cwd=repo_dir)
    if remote_out.strip():
        raise PreflightError(
            f"tag {tag!r} already exists on remote {remote!r} -- tags are never recreated"
        )


# --------------------------------------------------------------------------
# Tag ref resolution
# --------------------------------------------------------------------------


def resolve_tag_sha(repo_dir: Path, tag: str, *, remote: str = "origin") -> str:
    """Resolve `tag` (must exist as an actual tag ref on `remote`, `v*`)
    to the exact commit SHA it names, peeling an annotated tag to its
    target commit. Raises `PreflightError` if the tag does not exist,
    or if the ref is ambiguous -- deliberately scoped to `refs/tags/*`
    only, so a same-named branch is never silently accepted."""
    if not tag.startswith("v"):
        raise PreflightError(f"malformed tag {tag!r}: must start with 'v'")
    # Two explicit refspecs, not a glob: `git ls-remote --tags <remote>
    # refs/tags/<tag>` alone omits an annotated tag's peeled (`^{}`)
    # target -- that only comes back when the peeled ref is *also* asked
    # for by name (confirmed the hard way against a real remote). A glob
    # (`refs/tags/<tag>*`) would get both lines too but risks matching an
    # unrelated same-prefixed tag (`v1.0.0` matching `v1.0.0-rc1`) --
    # exact refspecs avoid that ambiguity entirely.
    output = _run_git(
        ["ls-remote", "--tags", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"], cwd=repo_dir
    )
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        raise PreflightError(f"tag {tag!r} not found on remote {remote!r} (no branch fallback)")

    peeled_sha: str | None = None
    plain_sha: str | None = None
    for line in lines:
        try:
            sha, ref = line.split("\t")
        except ValueError as exc:
            raise PreflightError(f"unparsable ls-remote line {line!r}") from exc
        if ref == f"refs/tags/{tag}^{{}}":
            if peeled_sha is not None:
                raise PreflightError(f"ambiguous: multiple peeled refs for tag {tag!r}")
            peeled_sha = sha
        elif ref == f"refs/tags/{tag}":
            if plain_sha is not None:
                raise PreflightError(f"ambiguous: multiple tag refs for tag {tag!r}")
            plain_sha = sha
        else:
            raise PreflightError(f"unexpected ref {ref!r} while resolving tag {tag!r}")

    # An annotated tag's peeled (`^{}`) commit SHA is the one that actually
    # matters (its own SHA is the *tag object*, not a commit) -- prefer it
    # when present; a lightweight tag has no peeled entry at all.
    resolved = peeled_sha or plain_sha
    if resolved is None:
        raise PreflightError(f"could not resolve tag {tag!r} to a commit SHA")
    return resolved


# --------------------------------------------------------------------------
# PyPI publish gate
# --------------------------------------------------------------------------


def pypi_publish_enabled(raw: str | None) -> bool:
    """Mirrors the exact `vars.PYPI_PUBLISH_ENABLED == 'true'` condition
    the release workflow (Task 2) uses -- absent or anything other than
    the literal string `"true"` means disabled. Never raises: an absent
    or disabled gate is a normal, expected state, not an error."""
    return raw == "true"


# --------------------------------------------------------------------------
# gh required-checks
# --------------------------------------------------------------------------

GhRunner = Callable[[Sequence[str]], str]


def _default_gh_runner(args: Sequence[str]) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=60, shell=False
    )
    if result.returncode != 0:
        raise PreflightError(
            f"gh {' '.join(args)} failed rc={result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


@dataclass(frozen=True)
class CheckRunResult:
    name: str
    status: str
    conclusion: str | None
    head_sha: str


def required_checks(
    repo: str, sha: str, *, gh_runner: GhRunner | None = None
) -> dict[str, CheckRunResult]:
    """Verify every name in `REQUIRED_CHECK_NAMES` has exactly one
    check-run on `sha`, `status == "completed"` and
    `conclusion == "success"`. Raises `PreflightError` listing every
    distinct problem (not just the first) on any missing, duplicate,
    stale-SHA, or non-successful check. Returns the per-name results on
    success."""
    runner = gh_runner or _default_gh_runner
    raw = runner(["api", f"repos/{repo}/commits/{sha}/check-runs"])
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PreflightError(f"gh api returned non-JSON output: {exc}") from exc

    runs = payload.get("check_runs")
    if not isinstance(runs, list):
        raise PreflightError(
            f"gh api check-runs response missing a 'check_runs' array: {payload!r}"
        )

    by_name: dict[str, list[Mapping[str, object]]] = {}
    for run in runs:
        name = run.get("name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(run)

    problems: list[str] = []
    results: dict[str, CheckRunResult] = {}
    for name in REQUIRED_CHECK_NAMES:
        matches = by_name.get(name, [])
        if not matches:
            problems.append(f"missing required check {name!r} on {sha}")
            continue
        if len(matches) > 1:
            problems.append(
                f"ambiguous: {len(matches)} check-runs named {name!r} on {sha} -- "
                f"expected exactly 1"
            )
            continue
        run = matches[0]
        run_sha = run.get("head_sha")
        status = run.get("status")
        conclusion = run.get("conclusion")
        if run_sha != sha:
            problems.append(
                f"stale SHA: check {name!r} reports head_sha={run_sha!r}, expected {sha!r}"
            )
            continue
        if status != "completed":
            problems.append(f"check {name!r} is not completed (status={status!r})")
            continue
        if conclusion != "success":
            problems.append(f"check {name!r} did not succeed (conclusion={conclusion!r})")
            continue
        results[name] = CheckRunResult(
            name=name, status=str(status), conclusion=str(conclusion), head_sha=str(run_sha)
        )

    if problems:
        raise PreflightError(
            f"required checks not satisfied on {sha}:\n" + "\n".join(f"  - {p}" for p in problems)
        )
    return results


# --------------------------------------------------------------------------
# Artifact inventory
# --------------------------------------------------------------------------

ARTIFACT_CLASSES: tuple[str, ...] = (
    "sdist", "wheel", "linux-binary", "macos-binary", "windows-binary",
)

_SDIST_RE = re.compile(r"^belay_mcp-(?P<version>.+)\.tar\.gz$")
_WHEEL_RE = re.compile(r"^belay_mcp-(?P<version>[^-]+)-.+\.whl$")


def classify_artifact(path: Path) -> str | None:
    """Return one of `ARTIFACT_CLASSES`, or `None` if `path` matches none
    of them (such a file is simply ignored, not an error by itself --
    `verify_artifacts` only cares that each of the five classes is
    covered exactly once). Binaries are distinguished by filename marker
    since a bare PyInstaller `belay`/`belay.exe` output carries no OS
    marker of its own -- the release workflow (Task 2) is responsible for
    renaming each OS's binary to embed one before upload; see this
    module's docstring."""
    name = path.name
    lower = name.lower()
    if _SDIST_RE.match(name):
        return "sdist"
    if _WHEEL_RE.match(name):
        return "wheel"
    if lower.endswith(".exe"):
        return "windows-binary" if "windows" in lower or "win" in lower else None
    if "belay" in lower:
        if "linux" in lower:
            return "linux-binary"
        if "macos" in lower or "darwin" in lower:
            return "macos-binary"
    return None


@dataclass(frozen=True)
class ArtifactInventory:
    by_class: dict[str, Path] = field(default_factory=dict)


def verify_artifacts(directory: Path, tag: str) -> ArtifactInventory:
    """Verify `directory` (searched recursively -- `actions/download-
    artifact` lays multi-artifact downloads out in per-artifact
    subdirectories) contains exactly one file for each of
    `ARTIFACT_CLASSES`, none zero-byte, and sdist/wheel filenames embed
    the exact PEP 440 version derived from `tag`. Raises `PreflightError`
    listing every distinct problem found."""
    tag_version = parse_tag_version(tag)
    expected_version = str(tag_version)

    if not directory.is_dir():
        raise PreflightError(f"artifact directory {directory} does not exist")

    by_class: dict[str, list[Path]] = {cls: [] for cls in ARTIFACT_CLASSES}
    for candidate in sorted(directory.rglob("*")):
        if not candidate.is_file():
            continue
        cls = classify_artifact(candidate)
        if cls is not None:
            by_class[cls].append(candidate)

    problems: list[str] = []
    resolved: dict[str, Path] = {}
    for cls in ARTIFACT_CLASSES:
        matches = by_class[cls]
        if not matches:
            problems.append(f"missing artifact class {cls!r} in {directory}")
            continue
        if len(matches) > 1:
            problems.append(
                f"ambiguous: {len(matches)} files match artifact class {cls!r}: "
                f"{[str(m) for m in matches]}"
            )
            continue
        artifact = matches[0]
        size = artifact.stat().st_size
        if size == 0:
            problems.append(f"zero-byte artifact for class {cls!r}: {artifact}")
            continue
        if cls in ("sdist", "wheel"):
            pattern = _SDIST_RE if cls == "sdist" else _WHEEL_RE
            match = pattern.match(artifact.name)
            assert match is not None  # guaranteed by classify_artifact
            found_version = match.group("version")
            if found_version != expected_version:
                problems.append(
                    f"version mismatch for {cls!r}: {artifact.name} embeds version "
                    f"{found_version!r}, expected {expected_version!r} (from tag {tag!r})"
                )
                continue
        resolved[cls] = artifact

    if problems:
        details = "\n".join(f"  - {p}" for p in problems)
        raise PreflightError(f"artifact inventory invalid in {directory}:\n{details}")
    return ArtifactInventory(by_class=resolved)


# --------------------------------------------------------------------------
# prepare: the full pre-tag gate
# --------------------------------------------------------------------------


def prepare(
    tag: str,
    repo: str,
    *,
    repo_dir: Path,
    pypi_publish_enabled_raw: str | None,
    gh_runner: GhRunner | None = None,
) -> dict[str, object]:
    """The full pre-tag gate (E23 Task 1 Step 6). Runs, in order:
    tag-format + tag/version consistency, clean worktree, local `main` ==
    `origin/main`, tag absent locally and on the remote, and -- resolving
    `origin/main`'s own current SHA as the release candidate -- every
    required check green on that exact SHA. Raises `PreflightError` on
    the first category to fail (unlike the sub-checks it composes, which
    each report every problem within themselves) since later steps
    genuinely depend on earlier ones (e.g. there is no meaningful
    candidate SHA to check required-checks against until the worktree
    state itself is verified sane)."""
    tag_version = check_version_consistency(
        tag,
        pyproject_path=repo_dir / "pyproject.toml",
        package_json_path=repo_dir / "npm" / "package.json",
    )
    check_clean_worktree(repo_dir)
    candidate_sha = check_local_matches_remote_main(repo_dir)
    check_tag_absent(repo_dir, tag)
    pypi_enabled = pypi_publish_enabled(pypi_publish_enabled_raw)
    checks = required_checks(repo, candidate_sha, gh_runner=gh_runner)

    return {
        "tag": tag,
        "version": str(tag_version),
        "repo": repo,
        "candidate_sha": candidate_sha,
        "pypi_publish_enabled": pypi_enabled,
        "required_checks": sorted(checks),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_error_and_exit(exc: PreflightError) -> int:
    print(f"release_preflight: FAILED -- {exc}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_resolve = sub.add_parser("resolve-tag", help="Resolve a tag to its exact commit SHA.")
    p_resolve.add_argument("--tag", required=True)
    p_resolve.add_argument("--repo-dir", default=str(REPO_ROOT))
    p_resolve.add_argument("--remote", default="origin")

    p_checks = sub.add_parser("required-checks", help="Verify all required check-runs on a SHA.")
    p_checks.add_argument("--repo", required=True, help="owner/repo, e.g. Jairogelpi/belay-mcp")
    p_checks.add_argument("--sha", required=True)

    p_artifacts = sub.add_parser(
        "verify-artifacts", help="Verify the five-class artifact inventory."
    )
    p_artifacts.add_argument("--tag", required=True)
    p_artifacts.add_argument("--directory", required=True)

    p_prepare = sub.add_parser("prepare", help="Run the full pre-tag gate.")
    p_prepare.add_argument("--tag", required=True)
    p_prepare.add_argument("--repo", required=True, help="owner/repo, e.g. Jairogelpi/belay-mcp")
    p_prepare.add_argument("--repo-dir", default=str(REPO_ROOT))

    args = parser.parse_args(argv)

    try:
        if args.command == "resolve-tag":
            sha = resolve_tag_sha(Path(args.repo_dir), args.tag, remote=args.remote)
            print(json.dumps({"tag": args.tag, "sha": sha}))
            return 0

        if args.command == "required-checks":
            results = required_checks(args.repo, args.sha)
            print(json.dumps({"repo": args.repo, "sha": args.sha, "checks": sorted(results)}))
            return 0

        if args.command == "verify-artifacts":
            inventory = verify_artifacts(Path(args.directory), args.tag)
            print(
                json.dumps(
                    {
                        "tag": args.tag,
                        "directory": args.directory,
                        "artifacts": {cls: str(p) for cls, p in inventory.by_class.items()},
                    }
                )
            )
            return 0

        if args.command == "prepare":
            import os

            summary = prepare(
                args.tag,
                args.repo,
                repo_dir=Path(args.repo_dir),
                pypi_publish_enabled_raw=os.environ.get("PYPI_PUBLISH_ENABLED"),
            )
            print(json.dumps(summary))
            return 0
    except PreflightError as exc:
        return _print_error_and_exit(exc)

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
