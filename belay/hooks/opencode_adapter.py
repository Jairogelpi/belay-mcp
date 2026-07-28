"""OpenCode host adapter (plan-v2 E18.6).

PREPARATORY, NOT WIRED UP TO A LIVE SESSION -- same honesty posture as
`belay/hooks/codex_adapter.py`'s own docstring, and for a related but
distinct reason. OpenCode's tool-call hooks (`tool.execute.before`/
`tool.execute.after`) are not a subprocess belay can invoke (Claude Code)
and not a JSON-RPC session belay can proxy (Codex) -- they're plain
function calls made *in-process*, by OpenCode's own compiled binary, into
whatever plugin module the user configured (a TypeScript/JavaScript file
loaded via Bun, e.g. `~/.config/opencode/plugins/*.ts`). There is no
Python-reachable seam here at all: gating OpenCode for real means shipping
an actual TS/JS plugin package that, from inside that in-process call,
talks out to belay's supervisor over the same wire protocol
`belay/supervisor/wire.py` already speaks -- a new language and packaging
surface for this repo, not just a new adapter module. That plugin does not
exist yet. This module is the Python-side normalize/render logic such a
plugin would eventually call out to reach, built and tested against the
real payload shape, so that work isn't starting from zero once someone
does build the TS side.

Verified, not guessed, and how: OpenCode ships no public schema command
(unlike Codex's `generate-json-schema`), so this was confirmed two
different ways against the actual installed `opencode-ai` 1.14.33 Windows
binary on this machine:

1. A real, already-installed third-party plugin at
   `~/.config/opencode/plugins/engram.ts` implements `"tool.execute.after":
   async (input, output) => ...` and actually reads `input.tool`,
   `input.sessionID` in production -- not a docs example, a plugin this
   user runs.
2. The compiled binary itself was searched for the literal trigger call
   sites (`grep`-style byte scan for `tool.execute.before`/`.after`
   inside the minified bundle) and both were found verbatim, e.g.:
   `W.trigger("tool.execute.before",{tool:DH.id,sessionID:GH.sessionID,
   callID:GH.callID},{args:IH})` and `W.trigger("tool.execute.after",
   {tool:DH.id,sessionID:GH.sessionID,callID:GH.callID,args:IH},NH)`
   (`NH` being the tool's own result object) -- confirming the exact
   `(input, output)` argument shape for both hooks directly from the
   shipped code, not from documentation that (as with Claude Code's
   PostToolUse) could have gone stale or been ambiguous.

Not verified, flagged rather than assumed: whether a plugin can actually
*deny* a call by throwing inside `tool.execute.before` (the surrounding
generator-based control flow makes that architecturally plausible --
`DH.execute(...)` runs only after the trigger `yield*` completes, in the
same sequential generator, so a thrown error there would plausibly abort
before execution -- but this was read from minified bundle code, not
proven by actually throwing inside a live plugin and observing the tool
call get skipped). Also not verified: the exact string OpenCode's built-in
tools use as their `tool` id beyond `"bash"` (found via a `BashTool`
symbol and an `id:"bash"` LSP-adapter string in the same binary, giving
reasonable but not ironclad confidence) -- `"edit"`/`"write"`/`"read"` for
the file surface are this module's inference from OpenCode's
publicly-known tool naming convention, not confirmed the same direct way
the hook shape was.
"""

from __future__ import annotations

import subprocess
from typing import Any

from belay.hooks.gate import GateDecision
from belay.supervisor.protocol import (
    SCHEMA_VERSION,
    HookEvent,
    Surface,
    TrustTier,
    local_os_user,
    now_fields,
)

ADAPTER_VERSION = "opencode/1"

_VERIFIED_TRUST_TIER: TrustTier | None = None


def _trust_tier() -> TrustTier:
    return _VERIFIED_TRUST_TIER if _VERIFIED_TRUST_TIER is not None else "UNKNOWN"


#: Not independently verified the way the hook shape itself was -- see the
#: module docstring's "Not verified" paragraph. "bash" has real supporting
#: evidence (a `BashTool` symbol, an `id:"bash"` string, both in the
#: installed binary); "edit"/"write"/"read" are inferred from OpenCode's
#: publicly-documented built-in tool set, not confirmed the same way.
_SURFACE_BY_TOOL_ID: dict[str, Surface] = {
    "bash": "shell",
    "edit": "file",
    "write": "file",
    "patch": "file",
    "read": "file",
}


def _surface_for(tool_id: str) -> Surface:
    return _SURFACE_BY_TOOL_ID.get(tool_id, "other")


def _repo_identity(cwd: str | None) -> str | None:
    """Same logic as `claude_code_adapter._repo_identity`/
    `codex_adapter._repo_identity` -- duplicated per-adapter deliberately,
    see either of those for why."""
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


def normalize_tool_execute_before(
    input_: dict[str, Any],
    output: dict[str, Any],
    *,
    installation_id: str,
    cwd: str | None = None,
) -> HookEvent:
    """`(input, output)` from a real `tool.execute.before` trigger call
    (`input = {tool, sessionID, callID}`, `output = {args}` -- confirmed
    shape, see module docstring) -> `HookEvent`. `cwd` is not part of
    either object in the confirmed call site, so it's accepted as a
    parameter for whatever future TS shim can obtain it some other way
    (e.g. the plugin's own `directory` context) -- `None` is a legitimate,
    honest answer when it isn't available, not a guess.
    """
    tool_id = input_.get("tool")
    if not isinstance(tool_id, str) or not tool_id:
        raise ValueError("tool.execute.before input.tool must be a non-empty string")
    session_id = input_.get("sessionID")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("tool.execute.before input.sessionID must be a non-empty string")
    call_id = input_.get("callID")

    args = output.get("args") if isinstance(output, dict) else None
    if not isinstance(args, dict):
        raise ValueError("tool.execute.before output.args must be an object")

    surface = _surface_for(tool_id)
    monotonic_ns, wall_clock = now_fields()

    normalized_args = {"command": args.get("command")} if surface == "shell" else args

    return HookEvent(
        schema_version=SCHEMA_VERSION,
        installation_id=installation_id,
        trust_tier=_trust_tier(),
        host="opencode",
        host_version=None,
        adapter_version=ADAPTER_VERSION,
        host_session_id=session_id,
        event_id=str(call_id or ""),
        phase="pre",
        surface=surface,
        tool_name="Bash" if surface == "shell" else tool_id,
        normalized_identity=tool_id,
        args=normalized_args,
        cwd=cwd,
        repo_identity=_repo_identity(cwd),
        os_user=local_os_user(),
        monotonic_ns=monotonic_ns,
        wall_clock=wall_clock,
        result_status=None,
        exit_code=None,
        output_digest=None,
        truncated=None,
    )


def render_tool_execute_before_decision(decision: GateDecision) -> dict[str, Any]:
    """`GateDecision` -> belay's own internal representation of what a
    (not-yet-built) TS shim should do inside a real `tool.execute.before`
    handler -- `{"action": "allow"}` means return normally, `{"action":
    "deny", "reason": ...}` means the shim should throw with that reason
    (see module docstring: throwing to block was not independently proven
    against a live session, only read from minified bundle control flow).
    This is NOT a verified on-wire format the way
    `claude_code_adapter.render_response`/`codex_adapter.render_*` are --
    OpenCode's hook mechanism has no wire format at all, it's an in-process
    function call, so there is nothing to conform to beyond whatever a
    real TS shim is eventually built to expect."""
    if decision.verdict == "allow":
        return {"action": "allow"}
    return {"action": "deny", "reason": decision.reason}


#: Field names OpenCode's own Bash tool result is publicly known to use
#: (`stdout`/`stderr`/`exitCode`/`exit_code` conventions), not confirmed
#: against the binary the way `tool.execute.before`'s shape was -- same
#: defensive, never-fabricate posture as
#: `claude_code_adapter._extract_post_result`'s documented uncertainty.
_EXIT_CODE_KEYS = ("exitCode", "exit_code", "returncode")
_STDOUT_KEYS = ("stdout", "output")
_STDERR_KEYS = ("stderr",)


def _first_present(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in d:
            return d[key]
    return None


def normalize_tool_execute_after(
    input_: dict[str, Any],
    output: Any,
    *,
    installation_id: str,
    cwd: str | None = None,
) -> HookEvent:
    """`(input, output)` from a real `tool.execute.after` trigger call
    (`input = {tool, sessionID, callID, args}`, `output` = the tool's own
    result, string or object -- confirmed shape, see module docstring)
    -> `HookEvent`."""
    import hashlib

    tool_id = input_.get("tool")
    if not isinstance(tool_id, str) or not tool_id:
        raise ValueError("tool.execute.after input.tool must be a non-empty string")
    session_id = input_.get("sessionID")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("tool.execute.after input.sessionID must be a non-empty string")
    call_id = input_.get("callID")
    raw_args = input_.get("args")
    args: dict[str, Any] = raw_args if isinstance(raw_args, dict) else {}

    surface = _surface_for(tool_id)
    monotonic_ns, wall_clock = now_fields()

    exit_code = stdout = stderr = None
    if isinstance(output, dict):
        exit_code = _first_present(output, _EXIT_CODE_KEYS)
        if not isinstance(exit_code, int):
            exit_code = None
        stdout = _first_present(output, _STDOUT_KEYS)
        stderr = _first_present(output, _STDERR_KEYS)

    output_digest = None
    material = None
    if isinstance(stdout, str) or isinstance(stderr, str):
        material = (stdout or "") + (stderr or "")
    elif isinstance(output, str):
        material = output
    if material is not None:
        output_digest = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    result_status = "success" if exit_code == 0 else ("failure" if exit_code is not None else None)

    return HookEvent(
        schema_version=SCHEMA_VERSION,
        installation_id=installation_id,
        trust_tier=_trust_tier(),
        host="opencode",
        host_version=None,
        adapter_version=ADAPTER_VERSION,
        host_session_id=session_id,
        event_id=str(call_id or ""),
        phase="post",
        surface=surface,
        tool_name="Bash" if surface == "shell" else tool_id,
        normalized_identity=tool_id,
        args={"command": args.get("command")} if surface == "shell" else args,
        cwd=cwd,
        repo_identity=_repo_identity(cwd),
        os_user=local_os_user(),
        monotonic_ns=monotonic_ns,
        wall_clock=wall_clock,
        result_status=result_status,
        exit_code=exit_code,
        output_digest=output_digest,
        truncated=None,
    )
