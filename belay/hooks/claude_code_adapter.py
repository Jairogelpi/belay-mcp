"""Claude Code host adapter: normalizes Claude Code's own hook stdin JSON
into the common `HookEvent` (spec §7.1) the gate's decision logic runs
against, and renders a `GateDecision` back into Claude Code's own expected
response JSON (`hookSpecificOutput.permissionDecision`).

Verified schema (fetched from code.claude.com/docs/en/hooks, the first-party
docs -- see conversation history for the exact fields checked): stdin JSON
carries `session_id`, `prompt_id`, `transcript_path`, `cwd`, `permission_mode`,
`hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, `effort`.
"""

from __future__ import annotations

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
#: review correctly called that an overclaim: spec TRUTH-004 says a host
#: integration is PROTECTED "only after its pinned-version end-to-end
#: bypass suite passes" (spec §7.2), and no such suite has run against a
#: real Claude Code binary yet (that's E18.7, separate future work; this
#: gate's own extensive local testing this session is real evidence the
#: *mechanism* works, but isn't the pinned-version conformance suite the
#: spec specifically requires before claiming T1). Reporting `"UNKNOWN"`
#: until that exists is the honest answer, not `"T0"` (which would
#: incorrectly claim no pre-execution blocking happens at all -- it does)
#: or a bare `"T1"` (which would claim more than has actually been proven).
_VERIFIED_TRUST_TIER: TrustTier | None = None


def _trust_tier() -> TrustTier:
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
    """The real git HEAD commit -- not merely "a `.git` directory exists
    here" (an earlier version returned the cwd path itself in that case,
    which identifies *a* repo but not *what commit it's at*; a P0 review
    correctly pointed out that binding an approval to the repository
    without its state lets the same approval silently cover a different
    commit/branch than the one it was actually granted for). `git
    rev-parse HEAD` only -- not the fuller `belay/ledger/test_evidence.py`
    `git_context()` (tree hash + dirty-worktree scan too), since that runs
    `git status --porcelain`, which can be slow on a large repo and this
    runs on every single hook decision (spec §16 latency budget), not once
    per verified test."""
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
    return result.stdout.strip() if result.returncode == 0 else None


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
    tool_input = raw.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = raw.get("cwd")
    monotonic_ns, wall_clock = now_fields()

    return HookEvent(
        schema_version=SCHEMA_VERSION,
        installation_id=installation_id,
        trust_tier=_trust_tier(),
        host="claude-code",
        host_version=None,  # not present in Claude Code's hook payload today
        adapter_version=ADAPTER_VERSION,
        host_session_id=str(raw.get("session_id") or ""),
        event_id=str(raw.get("tool_use_id") or ""),
        phase=phase,
        surface=_surface_for(tool_name),
        tool_name=tool_name,
        normalized_identity=tool_name,
        args=tool_input,
        cwd=cwd,
        repo_identity=_repo_identity(cwd),
        os_user=local_os_user(),
        monotonic_ns=monotonic_ns,
        wall_clock=wall_clock,
    )


def render_response(decision: GateDecision) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision.verdict,
            "permissionDecisionReason": decision.reason,
        }
    }
