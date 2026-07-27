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

    Only steps whose `plan_created.args` has a `path` key are considered;
    `content` present -> write (after=content), absent -> delete
    (after=None). The nearest preceding `state_captured` for that step_seq
    supplies `before` (its snapshot's `content`, if any).
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
        before = captures.get(step_seq, {}).get("content")
        after = args.get("content") if "content" in args else None
        changes.append(
            FileChange(step_seq=step_seq, tool=plan["tool"], path=path, before=before, after=after)
        )
    return changes


def apply_changes(repo: Path, changes: list[FileChange]) -> list[str]:
    """Write `changes` onto the working tree (caller has already checked out the PR branch)."""
    touched = []
    for change in changes:
        target = repo / change.path
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
    _run("git", "fetch", "origin", base, cwd=repo)
    _run("git", "checkout", "-B", branch, f"origin/{base}", cwd=repo)
    if not paths:
        return
    _run("git", "add", *paths, cwd=repo)
    _run("git", "commit", "-m", message, cwd=repo)


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

    sections.append(
        "\n### What was asked?\n" + (intent if intent else "_not declared (no intent contract given)_")
    )

    deviations = []
    if allowed_scope:
        for c in changes:
            if not any(fnmatch(c.path, pattern) for pattern in allowed_scope):
                deviations.append(c.path)
    sections.append(
        "\n### What changed without being asked?\n"
        + (
            "\n".join(f"- `{p}` (outside declared allowed_scope)" for p in deviations)
            if deviations
            else ("_none — every changed file is inside the declared scope_" if allowed_scope else "_not checked (no intent contract given)_")
        )
    )

    behavior_lines = [
        f"- step {c.step_seq}: {'delete' if c.after is None else 'write'} `{c.path}`"
        + (f" (intent: {by_step[c.step_seq].intent_id})" if by_step.get(c.step_seq) and by_step[c.step_seq].intent_id else "")
        for c in changes
    ]
    sections.append("\n### What new behavior exists?\n" + "\n".join(behavior_lines))

    proven = [n for n in nodes if n.test_ref]
    unproven = [n for n in nodes if not n.test_ref]
    sections.append(
        "\n### What was verified?\n"
        + (
            "\n".join(f"- step {n.step_seq} ({n.tool}): `{n.test_ref}`" for n in proven)
            or "_none of this session's steps carry a `_belay_test_ref`_"
        )
    )
    sections.append(
        "\n### What couldn't be proven?\n"
        + (
            "\n".join(f"- step {n.step_seq} ({n.tool}): no test reference attached" for n in unproven)
            or "_every step carries a test reference_"
        )
    )

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
