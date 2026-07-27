"""`belay export-pr`: package a committed session's file changes as a real git PR.

Post-hoc, not a pre-execution gate: the session already ran through the full
governed pipeline (contract -> plan -> policy -> approval -> saga commit,
spec §3-§8) and is sitting in the ledger. This reads what actually changed
-- per committed step, the file's captured "before" state (`state_captured`
snapshot) and the write's "after" content (`plan_created` args) -- and turns
it into a branch + commit + signed-evidence attachment + PR, so a change an
agent made to files under git is reviewable the way a human's change would
be, with Belay's own signed evidence (E13) as the paper trail rather than a
trust-me summary.

Deliberately narrow: only recognizes the read/write/delete-file shape
already used by `examples/contracts/fs.yaml` (a `path` arg, optionally
`content`) -- the same contract vocabulary Belay already ships, not a new
one. A tool whose args don't fit that shape is skipped, not guessed at.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from belay.ledger.model import Event


@dataclass(frozen=True)
class FileChange:
    step_seq: int
    tool: str
    path: str
    before: str | None  # None: file didn't exist / no capture
    after: str | None  # None: deleted


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def extract_file_changes(events: list[Event]) -> list[FileChange]:
    """Reconstruct per-step file diffs from a session's ledger events.

    Only steps whose declared `effects` (from the plan, i.e. the contract
    the step actually resolved against -- spec §4/§5) include an `update`
    or `delete` are considered a file change; a `read` effect never is,
    regardless of whether `content` happens to be absent from its args.
    Presence/absence of `content` alone is NOT the signal (a
    read-only call has no `content` either and must never be read as a
    delete) -- this was a real bug in an earlier version, caught by review:
    `fs.read_file(path=...)` was silently classified as deleting that path.
    """
    plans: dict[int, dict[str, Any]] = {}
    captures: dict[int, dict[str, Any]] = {}
    committed: set[int] = set()
    for event in events:
        if event.step_seq is None:
            continue
        if event.type == "plan_created":
            plans[event.step_seq] = event.payload
        elif event.type == "state_captured":
            captures[event.step_seq] = event.payload.get("snapshot") or {}
        elif event.type == "step_committed":
            committed.add(event.step_seq)

    changes: list[FileChange] = []
    for step_seq in sorted(committed):
        plan = plans.get(step_seq)
        if plan is None:
            continue
        args = plan.get("args") or {}
        path = args.get("path")
        if not isinstance(path, str):
            continue
        effect_types = {e.get("type") for e in plan.get("effects") or []}
        if "delete" in effect_types:
            after = None
        elif "update" in effect_types:
            after = args.get("content")
        else:
            continue  # read/execute/etc. -- not a file change, regardless of args shape
        before = captures.get(step_seq, {}).get("content")
        changes.append(
            FileChange(step_seq=step_seq, tool=plan["tool"], path=path, before=before, after=after)
        )
    return changes


class ExportPrError(Exception):
    """A real, checked failure in `export-pr`'s git/filesystem steps -- never silent."""


def _confine_to_repo(repo: Path, change_path: str) -> Path:
    """Resolve `change_path` under `repo` and refuse it if it escapes (same guard as
    `belay/intent/enforce.py`'s `path_escapes_scope` -- export-pr writes real files
    on disk, so a path that resolves outside the repo must never be silently followed."""
    target = (repo / change_path).resolve()
    repo_resolved = repo.resolve()
    if repo_resolved not in target.parents and target != repo_resolved:
        raise ExportPrError(f"refusing to touch path outside repo: {change_path!r}")
    return target


def apply_changes(repo: Path, changes: list[FileChange]) -> list[str]:
    """Write `changes` onto the working tree (caller has already checked out the PR branch).

    Requires a clean worktree first -- applying changes onto uncommitted
    local edits would silently fold them into the same commit, misattributing
    a human's in-progress work to the agent's session.
    """
    status = _run("git", "status", "--porcelain", cwd=repo)
    if status.returncode != 0:
        raise ExportPrError(f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip():
        raise ExportPrError(
            f"{repo} has uncommitted changes -- commit or stash them before export-pr"
        )

    touched = []
    for change in changes:
        target = _confine_to_repo(repo, change.path)
        if change.after is None:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.after, encoding="utf-8")
        touched.append(change.path)
    return touched


def create_branch_and_commit(
    repo: Path, branch: str, base: str, message: str, paths: list[str]
) -> None:
    checkout_steps: list[tuple[str, ...]] = [
        ("git", "fetch", "origin", base),
        ("git", "checkout", "-B", branch, f"origin/{base}"),
    ]
    for args in checkout_steps:
        result = _run(*args, cwd=repo)
        if result.returncode != 0:
            raise ExportPrError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    if not paths:
        return
    commit_steps: list[tuple[str, ...]] = [
        ("git", "add", *paths),
        ("git", "commit", "-m", message),
    ]
    for args in commit_steps:
        result = _run(*args, cwd=repo)
        if result.returncode != 0:
            raise ExportPrError(f"{' '.join(args)} failed: {result.stderr.strip()}")


def gh_pr_create_command(branch: str, base: str, title: str, body_file: str) -> list[str]:
    return [
        "gh", "pr", "create",
        "--head", branch,
        "--base", base,
        "--title", title,
        "--body-file", body_file,
    ]


def build_proof_body(
    session_id: str,
    events: list[Event],
    changes: list[FileChange],
    evidence_note: str,
    intent: str | None,
    allowed_scope: list[str] | None,
    rewind_plan_lines: list[str] | None,
) -> str:
    """A "proof-carrying PR" body answering a reviewer's actual questions, not just a diff list.

    Every section is either real ledger data or an explicit "not available"
    -- no section fabricates an answer it doesn't have the data to back.
    """
    from fnmatch import fnmatch

    from belay.cli.causal import build_causal_graph

    nodes = build_causal_graph(events)
    by_step = {n.step_seq: n for n in nodes}

    sections = [
        f"Automated PR from Belay session `{session_id}` (spec §3-§8: contract -> "
        f"plan -> policy -> approval -> saga commit already ran; this is the "
        f"paper trail, not a new gate)."
    ]

    asked_text = intent if intent else "_not declared (no intent contract given)_"
    sections.append("\n### What was asked?\n" + asked_text)

    deviations = []
    if allowed_scope:
        for c in changes:
            if not any(fnmatch(c.path, pattern) for pattern in allowed_scope):
                deviations.append(c.path)
    if deviations:
        deviations_text = "\n".join(f"- `{p}` (outside declared allowed_scope)" for p in deviations)
    elif allowed_scope:
        deviations_text = "_none — every changed file is inside the declared scope_"
    else:
        deviations_text = "_not checked (no intent contract given)_"
    sections.append("\n### What changed without being asked?\n" + deviations_text)

    behavior_lines = []
    for c in changes:
        node = by_step.get(c.step_seq)
        kind = "delete" if c.after is None else "write"
        suffix = f" (intent: {node.intent_id})" if node and node.intent_id else ""
        behavior_lines.append(f"- step {c.step_seq}: {kind} `{c.path}`{suffix}")
    sections.append("\n### What new behavior exists?\n" + "\n".join(behavior_lines))

    proven = [n for n in nodes if n.test_ref]
    unproven = [n for n in nodes if not n.test_ref]
    verified_text = (
        "\n".join(f"- step {n.step_seq} ({n.tool}): `{n.test_ref}`" for n in proven)
        or "_none of this session's steps carry a `_belay_test_ref`_"
    )
    sections.append("\n### What was verified?\n" + verified_text)
    unproven_text = (
        "\n".join(f"- step {n.step_seq} ({n.tool}): no test reference attached" for n in unproven)
        or "_every step carries a test reference_"
    )
    sections.append("\n### What couldn't be proven?\n" + unproven_text)

    effects = []
    for event in events:
        if event.type == "plan_created" and event.step_seq is not None:
            for eff in event.payload.get("effects") or []:
                effects.append(f"- step {event.step_seq}: {eff.get('type')} {eff.get('resource')}")
    sections.append(
        "\n### What external effects occurred?\n" + ("\n".join(effects) or "_none recorded_")
    )

    sections.append(
        "\n### How is this undone?\n"
        + (
            "```\n" + "\n".join(rewind_plan_lines) + "\n```"
            if rewind_plan_lines
            else "_not computed (pass --config to include a real `belay rewind --dry-run` plan)_"
        )
    )

    if evidence_note:
        sections.append(evidence_note)

    return "\n".join(sections)
