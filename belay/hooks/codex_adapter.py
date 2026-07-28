"""Codex CLI host adapter (plan-v2 E18.5).

PREPARATORY, NOT WIRED UP TO A LIVE SESSION -- said plainly, matching how
`belay/hooks/claude_code_adapter.py`'s own docstring flags what wasn't
independently verified. This module normalizes Codex's approval-request
message shapes into the common `HookEvent` the gate already runs against
(zero changes to `belay/hooks/gate.py` or `belay/supervisor/server.py`
were needed -- the host-agnostic design pays off exactly as intended), and
renders a `GateDecision` back into Codex's own expected response shape.
But nothing in belay actually spawns or proxies a live `codex app-server`
JSON-RPC session yet, so these functions are not reachable from any real
Codex run today.

Why that's a materially bigger gap than it sounds: Claude Code's hook is a
one-shot subprocess invoked synchronously with JSON on stdin, JSON on
stdout, and it's done (`belay/hooks/claude_code_adapter.py`). Codex's
approval mechanism is a *bidirectional* JSON-RPC protocol
(`codex app-server`) inside a long-lived session -- intercepting it for
real means belay either drives the whole session itself or sits as a
transparent proxy between the real client and the real `codex app-server`
process, forwarding every other message untouched and only intercepting
approval requests. That's genuinely different infrastructure (closer in
shape to `belay wrap`'s MCP stdio proxy than to `belay hooks run`), and
building it without running it end-to-end against a real session would
repeat exactly the mistake this project has avoided elsewhere (see
`claude_code_adapter.py`'s own PostToolUse field-name note) -- shipping an
unverified guess dressed up as a working integration. So: this slice stops
at "the normalize/render logic is real and tested against the actual
schema the installed binary emits," not "Codex calls are gated."

Verified, not guessed: every field name and shape below was read straight
from `codex app-server generate-json-schema --experimental` run against
the real installed `codex-cli 0.145.0` binary on this machine (not from
docs, which for Claude Code's PostToolUse gave conflicting answers -- this
time there was a real binary to ask instead). Two approval request types
exist at the protocol's stable (v1-era) level:

- `ExecCommandApprovalParams` (shell surface): `callId`, `command` (an
  argv array -- Codex, unlike Claude Code's Bash tool, never hands over a
  single shell string), `cwd`, `parsedCmd`, `conversationId`, optional
  `reason`/`approvalId`. Answered with `ExecCommandApprovalResponse
  {"decision": ReviewDecision}`.
- `ApplyPatchApprovalParams` (file surface): `callId`, `fileChanges` (a
  path -> add/delete/update map -- a single approval can cover several
  files at once, unlike Claude Code's one-file-per-call Edit/Write),
  `conversationId`, optional `grantRoot`/`reason`. Answered with
  `ApplyPatchApprovalResponse {"decision": ReviewDecision}` -- confirmed
  identical shape to `ExecCommandApprovalResponse`, not assumed from the
  matching name alone.

Also discovered, and deliberately NOT targeted here: a second, newer
"Guardian" approval concept exists under the protocol's `v2/` namespace
(`ItemGuardianApprovalReviewStartedNotification`,
`ThreadApproveGuardianDeniedActionParams`) alongside the classic
`ExecCommandApproval`/`ApplyPatchApproval` pair used here. Which one a
live client actually receives depends on the protocol version negotiated
at `Initialize`, and that negotiation was never exercised against a real
running session -- flagged rather than silently picking one and hoping.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from belay.hooks.gate import GateDecision
from belay.supervisor.protocol import (
    SCHEMA_VERSION,
    HookEvent,
    TrustTier,
    local_os_user,
    now_fields,
)

ADAPTER_VERSION = "codex/1"

#: Same honesty as claude_code_adapter.py's own `_VERIFIED_TRUST_TIER`:
#: nothing here has passed a pinned-version end-to-end bypass suite (spec
#: §7.2) -- and unlike Claude Code, this adapter isn't even wired to a live
#: session yet, so `UNKNOWN` is doubly appropriate.
_VERIFIED_TRUST_TIER: TrustTier | None = None


def _trust_tier() -> TrustTier:
    return _VERIFIED_TRUST_TIER if _VERIFIED_TRUST_TIER is not None else "UNKNOWN"


def _repo_identity(cwd: str | None) -> str | None:
    """Same logic as `claude_code_adapter._repo_identity` (real `git
    rev-parse HEAD`, not merely "a .git directory exists") -- duplicated
    rather than imported across adapters so each host adapter stays
    independently readable and one adapter's change can never accidentally
    ripple into another's already-verified behavior."""
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


def normalize_exec_command_approval(
    params: dict[str, Any], *, installation_id: str, host_session_id: str
) -> HookEvent:
    """`ExecCommandApprovalParams` -> `HookEvent`. Reuses `tool_name="Bash"`/
    `surface="shell"` even though Codex itself never uses that name -- that
    is exactly what lets `gate.evaluate()`'s existing Bash classifier run
    against it completely unmodified. Raises `ValueError` on a malformed
    payload -- never guesses a command from a partial/wrong-shaped params
    object.
    """
    command_argv = params.get("command")
    if not isinstance(command_argv, list) or not command_argv or not all(
        isinstance(c, str) for c in command_argv
    ):
        raise ValueError("ExecCommandApprovalParams.command must be a non-empty list of strings")

    # Codex hands over argv, not a shell string -- classify_bash and its
    # allowlist/metacharacter checks operate on shell text, so this
    # reconstructs one via shlex.join (POSIX-correct quoting), never a
    # naive " ".join that could misrepresent an argument containing spaces
    # or shell metacharacters as something safer than it is.
    command = shlex.join(command_argv)
    cwd = params.get("cwd")
    monotonic_ns, wall_clock = now_fields()

    return HookEvent(
        schema_version=SCHEMA_VERSION,
        installation_id=installation_id,
        trust_tier=_trust_tier(),
        host="codex",
        host_version=None,
        adapter_version=ADAPTER_VERSION,
        host_session_id=host_session_id,
        event_id=str(params.get("callId") or ""),
        phase="pre",
        surface="shell",
        tool_name="Bash",
        normalized_identity="Bash",
        args={"command": command},
        cwd=cwd if isinstance(cwd, str) else None,
        repo_identity=_repo_identity(cwd if isinstance(cwd, str) else None),
        os_user=local_os_user(),
        monotonic_ns=monotonic_ns,
        wall_clock=wall_clock,
        result_status=None,
        exit_code=None,
        output_digest=None,
        truncated=None,
    )


def render_exec_command_approval_response(decision: GateDecision) -> dict[str, Any]:
    """`GateDecision` -> `ExecCommandApprovalResponse` (verified shape:
    `{"decision": ReviewDecision}`). Only the two ReviewDecision variants
    this adapter ever needs are produced -- the plain string `"approved"`
    and the structured `{"denied": {"rejection": <reason>}}` -- never
    `"approved_for_session"` (that would silently cover every future
    command in the session, not just the one context this decision was
    actually made for -- the same reasoning that keeps Bash's own approval
    binding scoped to full context, not the command alone) and never
    `"abort"` (that stops the whole session, a much bigger action than
    belay denying one call)."""
    if decision.verdict == "allow":
        return {"decision": "approved"}
    return {"decision": {"denied": {"rejection": decision.reason}}}


#: Codex's `fileChanges` map can name several files in one patch; belay's
#: file-edit gate (`gate.evaluate_file_edit`/`SnapshotStore`) captures one
#: path per `HookEvent`, matching Claude Code's one-file-per-call Edit/
#: Write/NotebookEdit shape. Normalizing to the first path (sorted, so the
#: choice is at least deterministic) rather than silently picking whichever
#: key iteration happened to return -- and said plainly: a real multi-file
#: patch would only get the first file captured for rewind, not all of
#: them. Full multi-file capture is out of scope for this slice.
def normalize_apply_patch_approval(
    params: dict[str, Any], *, installation_id: str, host_session_id: str
) -> HookEvent:
    file_changes = params.get("fileChanges")
    if not isinstance(file_changes, dict) or not file_changes:
        raise ValueError("ApplyPatchApprovalParams.fileChanges must be a non-empty object")

    path = sorted(file_changes)[0]
    monotonic_ns, wall_clock = now_fields()

    return HookEvent(
        schema_version=SCHEMA_VERSION,
        installation_id=installation_id,
        trust_tier=_trust_tier(),
        host="codex",
        host_version=None,
        adapter_version=ADAPTER_VERSION,
        host_session_id=host_session_id,
        event_id=str(params.get("callId") or ""),
        phase="pre",
        surface="file",
        tool_name="Write",
        normalized_identity="Write",
        args={"file_path": path},
        cwd=None,  # ApplyPatchApprovalParams carries no cwd -- paths are as given
        repo_identity=None,
        os_user=local_os_user(),
        monotonic_ns=monotonic_ns,
        wall_clock=wall_clock,
        result_status=None,
        exit_code=None,
        output_digest=None,
        truncated=None,
    )


def render_apply_patch_approval_response(decision: GateDecision) -> dict[str, Any]:
    """`GateDecision` -> `ApplyPatchApprovalResponse` -- same verified
    `{"decision": ReviewDecision}` shape as `ExecCommandApprovalResponse`,
    confirmed from the schema rather than assumed from the matching name."""
    if decision.verdict == "allow":
        return {"decision": "approved"}
    return {"decision": {"denied": {"rejection": decision.reason}}}
