"""Claude Code host adapter: normalizes Claude Code's own hook stdin JSON
into the common `HookEvent` (`belay/supervisor/protocol.py` -- not a
`docs/spec.md` section, see `docs/adr/0020-extended-requirement-catalog.md`)
the gate's decision logic runs against, and renders a `GateDecision` back
into Claude Code's own expected
response JSON (`hookSpecificOutput.permissionDecision`).

Verified schema (fetched from code.claude.com/docs/en/hooks, the first-party
docs -- see conversation history for the exact fields checked): stdin JSON
carries `session_id`, `prompt_id`, `transcript_path`, `cwd`, `permission_mode`,
`hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, `effort`.
"""

from __future__ import annotations

import hashlib
import subprocess
from typing import Any

from belay.hooks.gate import GateDecision
from belay.supervisor.protocol import (
    SCHEMA_VERSION,
    HookEvent,
    Phase,
    Surface,
    TrustTier,
    local_os_user,
    now_fields,
)

ADAPTER_VERSION = "claude-code/1"

#: An earlier version hardcoded `trust_tier="T1"` on every event -- a P0
#: review correctly called that an overclaim: TRUTH-004 (see
#: docs/adr/0020-extended-requirement-catalog.md; not a docs/spec.md
#: section) says a host integration is PROTECTED only after its
#: pinned-version end-to-end bypass suite passes. That suite now exists
#: and passes:
#: `tests/hooks/test_live_conformance.py` (E18.7) spawns the real,
#: installed `claude` CLI (pinned to `PINNED_CLAUDE_VERSION` there --
#: currently 2.1.219, skips rather than claiming conformance against an
#: unverified version), with belay's hooks actually installed and Claude's
#: own permission layer bypassed so the result is attributable to belay's
#: hook alone, and confirms two independent ways per scenario: an
#: unrecognized Bash command's side effect genuinely never happens on
#: disk (not just "the model said it didn't run it") and a real pending
#: item lands in the actual approval queue, while a safe command still
#: reaches the real host and returns real output. That is the pinned-
#: version end-to-end bypass suite TRUTH-004 asks for, so `"T1"` here is
#: no longer an overclaim the way the earlier hardcoded version was --
#: it's specifically scoped to Claude Code's Bash surface (what the suite
#: actually exercises), not a claim about Edit/Write/MCP surfaces or any
#: other host.
_VERIFIED_TRUST_TIER: TrustTier | None = "T1"


def _trust_tier(surface: Surface) -> TrustTier:
    """`_VERIFIED_TRUST_TIER` only ever reflects what
    `tests/hooks/test_live_conformance.py` (E18.7) actually exercises:
    Claude Code's Bash surface. An earlier version applied it to every
    event regardless of `surface` -- a real overclaim, caught while
    building the adapter compatibility matrix
    (docs/adapter-compatibility.md): the ledger would have recorded
    `trust_tier: "T1"`
    for an Edit/Write or native MCP event too, even though no conformance
    suite has ever run against those surfaces. Only `"shell"` gets the
    verified tier; every other surface honestly reports `UNKNOWN` here,
    same as an entirely unverified host adapter would."""
    if surface != "shell":
        return "UNKNOWN"
    return _VERIFIED_TRUST_TIER if _VERIFIED_TRUST_TIER is not None else "UNKNOWN"

_PHASE_BY_HOOK_EVENT_NAME: dict[str, Phase] = {
    "PreToolUse": "pre",
    "PostToolUse": "post",
}

#: Ordered (checked first match wins) -- "Bash" is an exact tool name,
#: "mcp__" is Claude Code's own prefix convention for scoped MCP tool names
#: (`mcp__server__tool`), everything else with a recognizable prefix maps to
#: `file`; unrecognized tool names fall through to `other`.
_SURFACE_BY_TOOL_PREFIX: tuple[tuple[str, Surface], ...] = (
    ("Bash", "shell"),
    ("mcp__", "mcp"),
    ("Edit", "file"),
    ("Write", "file"),
    ("Read", "file"),
    ("NotebookEdit", "file"),
)


def _surface_for(tool_name: str) -> Surface:
    for prefix, surface in _SURFACE_BY_TOOL_PREFIX:
        if tool_name == prefix or tool_name.startswith(prefix):
            return surface
    return "other"


def _repo_identity(cwd: str | None) -> str | None:
    """The real git HEAD commit, plus a real content-based prestate digest
    (R1.6, revised post-review) -- not merely "a `.git` directory exists
    here" (an earlier version returned the cwd path itself in that case,
    which identifies *a* repo but not *what commit it's at*; a P0 review
    correctly pointed out that binding an approval to the repository
    without its state lets the same approval silently cover a different
    commit/branch than the one it was actually granted for). `git
    rev-parse HEAD` plus `_prestate_digest` below -- not the fuller
    `belay/ledger/test_evidence.py` `git_context()`'s exact machinery, but
    covering the same ground: a hash of the actual tracked-file diff
    content plus the untracked-file listing, not just a dirty/clean
    boolean.

    R1.6 first shipped this as a bare boolean (`git diff-index --quiet
    HEAD --`), which a post-merge review correctly flagged: two DIFFERENT
    uncommitted edits both collapse to the same `:dirty` suffix, so an
    approval granted against one dirty state could in principle still
    resolve for a different one. `_prestate_digest` fixes that by hashing
    the actual diff text and untracked-file listing instead of a bool --
    two different edits now produce two different identities. Still not
    a perfect prestate hash: an untracked file's own *content* isn't
    read (only its path, via `git status --porcelain`), so two different
    untracked files with the same name but different content still
    collide -- a real, acknowledged remaining gap, not silently claimed
    fixed. A fully exact prestate digest (every byte that could affect
    the decision) is real follow-up work for R1.7's canonical protocol,
    not attempted here."""
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return f"{head}:{_prestate_digest(cwd)}"


def _prestate_digest(cwd: str) -> str:
    """Hash of the actual working-tree content that differs from `HEAD`:
    `git diff HEAD --` (line-level content of every tracked-file change)
    plus `git status --porcelain=v1 --untracked-files=normal` (which
    files are untracked, by path -- not their content, see
    `_repo_identity`'s docstring for that residual gap).
    `--untracked-files=normal` (not `=all`) deliberately: for an entirely
    untracked directory it reports the directory itself rather than
    recursing into every file inside it, keeping this cheaper than a
    full recursive scan on a large repo with e.g. an untracked build
    output directory. Two `git` calls, not one -- `status --porcelain`
    alone reports M/A/D status codes but never a change's actual line
    content, so it can't tell two different edits to the same file
    apart; `diff HEAD` supplies that. A clean tree hashes the same fixed
    value regardless of HEAD (both commands produce empty output),
    which is correct: nothing to distinguish. Any git invocation failure
    returns a fixed `"unknown"` digest -- distinct from a real hash, and
    guaranteed to differ between two calls only by coincidence, so it
    fails toward requiring a fresh approval rather than silently trusting
    an unchecked worktree state."""
    try:
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if diff.returncode != 0 or status.returncode != 0:
        return "unknown"
    material = diff.stdout + "\x00" + status.stdout
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


#: Field name Claude Code actually uses for a PostToolUse result object.
#: NOT independently verified against a real Claude Code invocation --
#: two different fetches of the same docs (and third-party writeups) gave
#: conflicting names, `tool_response` vs `tool_result`, for what should be
#: the same field. Both are tried, in this order, rather than committing to
#: one and silently recording nothing if the real payload uses the other.
#: Flagged plainly rather than assumed correct: verify against a real
#: PostToolUse invocation before trusting the *sub*-field names below
#: (exit_code/stdout/stderr) for anything beyond "we recorded whatever
#: shape actually arrived."
_RESULT_FIELD_CANDIDATES = ("tool_response", "tool_result")
_EXIT_CODE_KEYS = ("exit_code", "exitCode", "returncode")
_STDOUT_KEYS = ("stdout",)
_STDERR_KEYS = ("stderr",)


def _first_present(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in d:
            return d[key]
    return None


def _extract_post_result(
    raw: dict[str, Any],
) -> tuple[str | None, int | None, str | None, bool | None]:
    """(result_status, exit_code, output_digest, truncated) -- best-effort,
    defensive extraction (see `_RESULT_FIELD_CANDIDATES`'s docstring for
    why this isn't fully certain). Never raises and never fabricates a
    value it didn't actually find: an absent/unrecognized field stays
    `None`, not a guessed default."""
    result_obj: Any = None
    for field_name in _RESULT_FIELD_CANDIDATES:
        candidate = raw.get(field_name)
        if isinstance(candidate, dict):
            result_obj = candidate
            break
    if result_obj is None:
        return None, None, None, None

    exit_code = _first_present(result_obj, _EXIT_CODE_KEYS)
    if not isinstance(exit_code, int):
        exit_code = None

    stdout = _first_present(result_obj, _STDOUT_KEYS)
    stderr = _first_present(result_obj, _STDERR_KEYS)
    output_digest = None
    if isinstance(stdout, str) or isinstance(stderr, str):
        material = (stdout or "") + (stderr or "")
        output_digest = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    truncated = result_obj.get("truncated")
    if not isinstance(truncated, bool):
        truncated = None

    result_status = None
    if exit_code is not None:
        result_status = "success" if exit_code == 0 else "failure"

    return result_status, exit_code, output_digest, truncated


def normalize(raw: dict[str, Any], *, installation_id: str) -> HookEvent:
    """Raises `ValueError` on an unrecognized `hook_event_name` -- an
    adapter that doesn't understand what it was just sent must never guess;
    the caller (the supervisor) turns that into a deny, never a crash that
    leaves the calling host hanging with no response at all."""
    hook_event_name = raw.get("hook_event_name", "")
    phase = _PHASE_BY_HOOK_EVENT_NAME.get(hook_event_name)
    if phase is None:
        raise ValueError(f"unrecognized Claude Code hook_event_name: {hook_event_name!r}")

    tool_name = str(raw.get("tool_name") or "")
    surface = _surface_for(tool_name)
    tool_input = raw.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = raw.get("cwd")
    monotonic_ns, wall_clock = now_fields()

    result_status = exit_code = output_digest = truncated = None
    if phase == "post":
        result_status, exit_code, output_digest, truncated = _extract_post_result(raw)

    return HookEvent(
        schema_version=SCHEMA_VERSION,
        installation_id=installation_id,
        trust_tier=_trust_tier(surface),
        host="claude-code",
        host_version=None,  # not present in Claude Code's hook payload today
        adapter_version=ADAPTER_VERSION,
        host_session_id=str(raw.get("session_id") or ""),
        event_id=str(raw.get("tool_use_id") or ""),
        phase=phase,
        surface=surface,
        tool_name=tool_name,
        normalized_identity=tool_name,
        args=tool_input,
        cwd=cwd,
        repo_identity=_repo_identity(cwd),
        os_user=local_os_user(),
        monotonic_ns=monotonic_ns,
        wall_clock=wall_clock,
        result_status=result_status,
        exit_code=exit_code,
        output_digest=output_digest,
        truncated=truncated,
    )


def render_response(decision: GateDecision | None) -> dict[str, Any]:
    """`decision is None` for PostToolUse: the tool already ran, so there is
    no allow/deny decision to render -- an empty object (exit 0, no
    specific fields) means "no opinion", which is exactly right: this call
    only records evidence, it was never going to change what already
    happened (spec/Claude Code docs: PostToolUse "cannot block the action
    -- it already happened")."""
    if decision is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision.verdict,
            "permissionDecisionReason": decision.reason,
        }
    }
