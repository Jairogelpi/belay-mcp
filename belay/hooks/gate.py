"""Host-agnostic PreToolUse decision logic -- wires
`belay.hooks.decision.classify_bash` to the existing `ApprovalQueue` (spec
§7) so a PAUSEd Bash command surfaces exactly where every other Belay pause
does: `belay approvals list/approve/reject`, never a native "ask" prompt the
agent's own client would show, which would let a human bypass Belay's queue
entirely -- the opposite of the point of routing through it (confirmed
product decision, plan-v2 E18).

Operates on the common `HookEvent` (spec §7.1), never a host's raw payload
shape directly -- a host adapter (`belay/hooks/claude_code_adapter.py`, and
whatever comes later for Codex) normalizes into `HookEvent` first and
renders `GateDecision` back into its own host's expected response JSON
after. This module never imports or knows about any specific host.

No new schema: `ApprovalQueue.request()` takes a free-form `session_id`/
`plan_id`/`plan` dict, so this reuses it as-is. `session_id` comes straight
from the normalized event's `host_session_id` -- no separate "hook session"
concept needed. `plan_id` is a deterministic hash of the full context the
command was proposed in (host, session, tool, command, cwd, repo HEAD, and
the decision-logic ruleset version -- see `_plan_id_for_event`), not the
command text alone: an earlier version hashed only the command string,
which a P0 review correctly flagged -- it let one approval silently cover
the identical command string run in a *different* repository, branch,
directory, or session than the one a human actually approved it for. A
second attempt at the identical command **in the identical context** finds
the same approval item instead of opening a new one -- and once a human
approves it, every future identical (command, context) pair is allowed
without asking again (deliberate: the human approved *this exact
situation*, not "trust this agent from now on").
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from belay.approvals.queue import ApprovalQueue
from belay.hooks.decision import DECISION_LOGIC_VERSION, Verdict, classify_bash
from belay.hooks.file_snapshot import MAX_SNAPSHOT_BYTES, SnapshotStore
from belay.supervisor.addressing import belay_home
from belay.supervisor.protocol import HookEvent


@dataclass(frozen=True)
class GateDecision:
    verdict: Literal["allow", "deny"]
    reason: str
    approval_id: str | None = None


def _resolve_arg(token: str, cwd: str | None) -> Path | None:
    try:
        candidate = Path(os.path.expanduser(token))
        if candidate.is_absolute():
            return candidate.resolve()
        base = Path(cwd) if cwd else Path.cwd()
        return (base / candidate).resolve()
    except (OSError, RuntimeError):
        return None


def _is_under(path: Path, base: Path) -> bool:
    # Path.relative_to is case-sensitive; Windows filesystems generally
    # aren't, so also compare normcased strings -- a command spelling the
    # path with different case must not slip past this on Windows.
    try:
        path.relative_to(base)
        return True
    except ValueError:
        p, b = os.path.normcase(str(path)), os.path.normcase(str(base))
        return p == b or p.startswith(b + os.sep)


def _touches_belay_home(command: str, cwd: str | None) -> bool:
    """Defense in depth, checked only after `classify_bash` has already
    allowlisted a command: even a "safe read" command must never be allowed
    to read belay's own internal paths -- the capability token, the private
    approvals database (`belay/supervisor/addressing.py`'s `belay_home()`).
    A P0 review correctly pointed out that "outside the project directory"
    does not mean "outside the OS user's own read permissions": Claude
    Code's Bash tool runs as that same user, so an otherwise-innocuous `cat`
    could be pointed at `~/.belay/keys/....key` and exfiltrate the token.

    Best-effort, not foolproof -- said plainly rather than implied (spec
    TRUTH-010: T1 does not resist an arbitrary same-user process; this
    closes the *obvious* path-argument case, not every conceivable one, e.g.
    reading it indirectly through a symlink this check doesn't happen to
    resolve, or a language runtime's own file APIs called from a command
    this classifier didn't recognize as reading a path at all). Tokenizing
    on whitespace is safe here specifically because `classify_bash` already
    rejected any shell metacharacter/quoting complexity before this ever
    runs -- there's no chaining or substitution left to misparse.
    """
    home = belay_home().resolve()
    for token in command.split():
        if token.startswith("-"):
            continue
        resolved = _resolve_arg(token, cwd)
        if resolved is not None and _is_under(resolved, home):
            return True
    return False


def _plan_id(kind: str, event: HookEvent, detail: str) -> str:
    """Binds an approval to the full context it was granted in, not just the
    proposed action's own text: host, session, tool, `detail` (the command
    string for Bash, a canonical JSON dump of the call's arguments for MCP),
    cwd, the repository's real git HEAD (see
    `claude_code_adapter._repo_identity`), and the decision-logic ruleset
    version (so a rule change never silently reinterprets an approval
    granted under the old rules). `kind` namespaces the hash so a Bash
    command and an MCP call can never collide even if their `detail` text
    happened to match by coincidence."""
    material = json.dumps(
        {
            "decision_logic_version": DECISION_LOGIC_VERSION,
            "host": event.host,
            "host_session_id": event.host_session_id,
            "tool_name": event.tool_name,
            "detail": detail,
            "cwd": event.cwd,
            "repo_identity": event.repo_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{kind}:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _plan_id_for_event(event: HookEvent, command: str) -> str:
    return _plan_id("bash", event, command)


def _plan_id_for_mcp(event: HookEvent) -> str:
    args_json = json.dumps(event.args, sort_keys=True, separators=(",", ":"))
    return _plan_id("mcp", event, args_json)


def evaluate(event: HookEvent, queue: ApprovalQueue) -> GateDecision:
    """Bash-specific classification -- `belay/supervisor/server.py`'s
    `_decide_pre` dispatches here only for `surface == "shell"`. Edit/Write/
    NotebookEdit go through `evaluate_file_edit`, native MCP calls through
    `evaluate_mcp_call`; anything else still falls through to this
    function's own guard below, which denies rather than silently allowing
    an unexamined tool call on a surface nothing yet classifies. See
    `belay/hooks/decision.py` for why Bash itself defaults to PAUSE, not
    allow.
    """
    if event.phase != "pre":
        return GateDecision("deny", f"belay: {event.phase}-phase events are not yet handled")

    if not event.event_id:
        # Spec §7.1: "Malformed, missing, duplicated, or ambiguous identity
        # on a mutating call MUST default to deny/pause." An event with no
        # correlation id can't be matched to its eventual post-phase
        # counterpart, so it's ambiguous by definition.
        return GateDecision("deny", "belay: missing event id -- ambiguous identity, denying")

    if event.surface != "shell" or event.tool_name != "Bash":
        return GateDecision(
            "deny",
            f"belay: {event.tool_name!r} ({event.surface}) is not yet handled by the "
            "Native Agent Gate -- denying rather than silently allowing an unexamined "
            "tool call",
        )

    command = event.args.get("command")
    if not isinstance(command, str):
        return GateDecision("deny", "belay: Bash call had no 'command' string")

    decision = classify_bash(command)
    if decision.verdict is Verdict.ALLOW:
        if _touches_belay_home(command, event.cwd):
            return GateDecision(
                "deny",
                "belay: command argument resolves into belay's own private storage -- "
                "denying even though the command itself is otherwise allowlisted "
                "(read access to belay's capability token/approvals data is never allowed)",
            )
        return GateDecision("allow", f"belay: {decision.reason}")

    plan_id = _plan_id_for_event(event, command)
    existing = queue.for_plan(plan_id)

    if existing is not None and existing.state == "approved":
        return GateDecision(
            "allow",
            f"belay: this exact command was already approved "
            f"(approval {existing.approval_id}, by {existing.approved_by})",
            approval_id=existing.approval_id,
        )
    if existing is not None and existing.state == "pending":
        return GateDecision(
            "deny",
            f"belay: still pending human approval (approval {existing.approval_id}) -- "
            "run `belay approvals list` / `belay approvals approve`, then retry the "
            "same command",
            approval_id=existing.approval_id,
        )
    if existing is not None and existing.state == "rejected":
        return GateDecision(
            "deny",
            f"belay: this exact command was rejected (approval {existing.approval_id}"
            f", by {existing.rejected_by}"
            + (f": {existing.reason}" if existing.reason else "")
            + ") -- a human must run a different command, this one won't be re-asked",
            approval_id=existing.approval_id,
        )

    # No open item (never requested, or the previous one expired -- expired
    # items are terminal too, so a fresh request is the only way forward):
    item = queue.request(
        session_id=event.host_session_id,
        plan_id=plan_id,
        # "tool" (not "tool_name"): matches the key `belay approvals list`
        # already reads via `item.plan.get('tool')` for the MCP path
        # (belay/planner/planner.py), so a hook-originated item displays
        # the same way in the same queue listing.
        plan={"tool": "Bash", "command": command, "reason": decision.reason},
    )
    return GateDecision(
        "deny",
        f"belay: {decision.reason} -- queued for human approval (approval {item.approval_id}) "
        "-- run `belay approvals list` / `belay approvals approve`, then retry the exact "
        "same command",
        approval_id=item.approval_id,
    )


#: `file_path` is the field name Claude Code's Edit/Write/NotebookEdit tools
#: are documented to use across multiple sources with reasonable
#: consistency (more consistent than the PostToolUse result field --
#: see claude_code_adapter.py's docstring on that ambiguity). `path` is
#: tried as a fallback in case a future tool or another host names it
#: differently -- never guessed beyond that, never fabricated.
_FILE_PATH_ARG_KEYS = ("file_path", "path")


def file_path_from_args(args: dict[str, Any]) -> str | None:
    for key in _FILE_PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def evaluate_file_edit(
    event: HookEvent, queue: ApprovalQueue, snapshots: SnapshotStore
) -> GateDecision:
    """Edit/Write/NotebookEdit calls are ALLOWED by default -- unlike Bash,
    where an unrecognized command pauses. Gating every routine file write
    for human approval would make this unusable for a normal coding
    session; the actual safety value here is being able to UNDO an edit
    afterward via `belay hooks rewind <event_id>`, which this captures a
    snapshot for as a side effect of allowing (spec §9.2 FILE-001).

    The one case that still pauses: a file too large to capture (spec
    FILE-006 -- "exceeding capture limits downgrades reversibility and
    normally requires approval"). Allowing that silently would make an
    edit that *can't* be undone look identical to one that can; ask a
    human instead of hiding the gap. Reuses the same ApprovalQueue
    plan_id-binding pattern as Bash's PAUSE path, scoped to this specific
    oversized file rather than the file's content (which, by definition,
    was never captured to bind to)."""
    if event.phase != "pre":
        return GateDecision("deny", f"belay: {event.phase}-phase events are not yet handled")
    if not event.event_id:
        return GateDecision("deny", "belay: missing event id -- ambiguous identity, denying")

    path_str = file_path_from_args(event.args)
    if path_str is None:
        reason = f"belay: {event.tool_name} call had no recognizable path argument"
        return GateDecision("deny", reason)

    snapshot = snapshots.capture_before(event.event_id, event.host_session_id, Path(path_str))
    if snapshot.state != "oversized":
        return GateDecision(
            "allow", f"belay: file edit captured for rewind (event {event.event_id})"
        )

    plan_id = "file-oversized:" + hashlib.sha256(
        f"{event.host}|{event.host_session_id}|{path_str}".encode()
    ).hexdigest()[:32]
    existing = queue.for_plan(plan_id)
    if existing is not None and existing.state == "approved":
        return GateDecision(
            "allow",
            f"belay: oversized file edit approved by a human (approval {existing.approval_id}) "
            "-- NOT reversible, no snapshot was captured",
            approval_id=existing.approval_id,
        )
    if existing is not None and existing.state == "pending":
        return GateDecision(
            "deny",
            f"belay: {path_str} exceeds the capture size cap ({MAX_SNAPSHOT_BYTES} bytes) -- "
            f"still pending human approval (approval {existing.approval_id})",
            approval_id=existing.approval_id,
        )
    if existing is not None and existing.state == "rejected":
        return GateDecision(
            "deny",
            f"belay: oversized edit of {path_str} was rejected (approval {existing.approval_id})"
            " -- this one won't be re-asked",
            approval_id=existing.approval_id,
        )

    item = queue.request(
        session_id=event.host_session_id,
        plan_id=plan_id,
        plan={
            "tool": event.tool_name,
            "path": path_str,
            "reason": f"file exceeds the {MAX_SNAPSHOT_BYTES}-byte capture cap -- "
            "editing it would not be reversible",
        },
    )
    return GateDecision(
        "deny",
        f"belay: {path_str} exceeds the capture size cap ({MAX_SNAPSHOT_BYTES} bytes) -- "
        f"cannot be made reversible; queued for human approval (approval {item.approval_id})",
        approval_id=item.approval_id,
    )


def evaluate_mcp_call(event: HookEvent, queue: ApprovalQueue) -> GateDecision:
    """Claude Code's native `mcp__<server>__<tool>` calls reach whatever MCP
    server is configured directly through the agent's own client -- a
    different code path from belay's own proxy (`belay wrap`/`belay run`),
    which enforces contracts (spec §4/§6: reversibility, effects,
    verification) on every call *it* receives, no matter who calls it. A
    call made this way never passes through that enforcement at all, so it
    gets no benefit-of-the-doubt exception even when the server name looks
    like it could be belay's own -- there is no reliable way for this
    host-agnostic module to confirm that without depending on
    client-config-file parsing (`belay/cli/client_configs.py`, a CLI-layer
    concern), and a wrong guess here would mean silently allowing an
    unaudited call. So every native MCP call gets the same treatment as an
    unrecognized Bash command: PAUSE, queued through the identical
    `ApprovalQueue`, bound to the full context (host, session, the specific
    `mcp__server__tool` identity, a canonical dump of its arguments, cwd,
    repo HEAD, ruleset version) the same way Bash's PAUSE path is -- a
    different argument set for the same tool is a different approval, not
    an automatic pass just because the tool name matched a prior one.
    """
    if event.phase != "pre":
        return GateDecision("deny", f"belay: {event.phase}-phase events are not yet handled")
    if not event.event_id:
        return GateDecision("deny", "belay: missing event id -- ambiguous identity, denying")

    plan_id = _plan_id_for_mcp(event)
    existing = queue.for_plan(plan_id)

    if existing is not None and existing.state == "approved":
        return GateDecision(
            "allow",
            f"belay: this exact MCP call ({event.tool_name}) was already approved "
            f"(approval {existing.approval_id}, by {existing.approved_by})",
            approval_id=existing.approval_id,
        )
    if existing is not None and existing.state == "pending":
        return GateDecision(
            "deny",
            f"belay: still pending human approval (approval {existing.approval_id}) -- "
            "run `belay approvals list` / `belay approvals approve`, then retry the "
            "same call",
            approval_id=existing.approval_id,
        )
    if existing is not None and existing.state == "rejected":
        return GateDecision(
            "deny",
            f"belay: this exact MCP call was rejected (approval {existing.approval_id}"
            f", by {existing.rejected_by}"
            + (f": {existing.reason}" if existing.reason else "")
            + ") -- a human must take a different action, this one won't be re-asked",
            approval_id=existing.approval_id,
        )

    item = queue.request(
        session_id=event.host_session_id,
        plan_id=plan_id,
        plan={
            "tool": event.tool_name,
            "args": event.args,
            "reason": "native MCP tool call, not routed through belay's own proxy "
            "-- not contract-checked",
        },
    )
    return GateDecision(
        "deny",
        f"belay: {event.tool_name} is a native MCP call outside belay's own proxy -- "
        f"queued for human approval (approval {item.approval_id}) -- run "
        "`belay approvals list` / `belay approvals approve`, then retry the exact same call",
        approval_id=item.approval_id,
    )


def ledger_session_id(event: HookEvent) -> str:
    """Groups every hook event for one host session into its own ledger
    session -- distinct from any `belay run` MCP session_id namespace (the
    `hook-<host>-` prefix), so the two can never collide even if a host
    happened to mint a session_id shaped like an MCP one."""
    return f"hook-{event.host}-{event.host_session_id}"


def pre_event_evidence(event: HookEvent, decision: GateDecision) -> dict[str, Any]:
    """Pure -- what belongs in the durable ledger for a PreToolUse decision
    (spec §12.1: policy/contract/pack/adapter versions, repository state,
    the actual verdict). The caller (the supervisor) owns writing this to
    `LedgerStore`; this function only shapes the payload."""
    return {
        "event_id": event.event_id,
        "host": event.host,
        "adapter_version": event.adapter_version,
        "decision_logic_version": DECISION_LOGIC_VERSION,
        "tool_name": event.tool_name,
        "surface": event.surface,
        "args": event.args,
        "cwd": event.cwd,
        "repo_identity": event.repo_identity,
        "verdict": decision.verdict,
        "reason": decision.reason,
        "approval_id": decision.approval_id,
    }


def post_event_evidence(event: HookEvent, *, duration_ms: float | None) -> dict[str, Any]:
    """Pure -- what belongs in the durable ledger for a PostToolUse result
    (spec §7.1: "result status, exit code, duration, output digest, and
    truncation flag for post events"). `duration_ms` is passed in rather
    than read from `event` because it can only be computed by whoever holds
    both this event's and its PreToolUse counterpart's monotonic timestamps
    -- a single stateless `normalize()` call never sees both."""
    return {
        "event_id": event.event_id,
        "host": event.host,
        "tool_name": event.tool_name,
        "result_status": event.result_status,
        "exit_code": event.exit_code,
        "duration_ms": duration_ms,
        "output_digest": event.output_digest,
        "truncated": event.truncated,
    }
