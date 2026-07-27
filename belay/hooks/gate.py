"""PreToolUse decision logic for `belay hooks run` -- wires
`belay.hooks.decision.classify_bash` to the existing `ApprovalQueue` (spec
§7) so a PAUSEd Bash command surfaces exactly where every other Belay pause
does: `belay approvals list/approve/reject`, never a native "ask" prompt the
agent's own client would show, which would let a human bypass Belay's queue
entirely -- the opposite of the point of routing through it (confirmed
product decision, plan-v2 E18).

No new schema: `ApprovalQueue.request()` takes a free-form `session_id`/
`plan_id`/`plan` dict, so this reuses it as-is. `session_id` comes straight
from the hook payload (Claude Code's own session id, already present on
every hook call -- no separate "hook session" concept needed).
`plan_id` is a deterministic hash of the exact command string, so a second
attempt at the identical command finds the same approval item instead of
opening a new one -- and once a human approves it, every future identical
command in that session is allowed without asking again (deliberate: the
human approved *this exact string*, not "trust this agent from now on").
"""

from __future__ import annotations

import hashlib
from typing import Any

from belay.approvals.queue import ApprovalQueue
from belay.hooks.decision import Verdict, classify_bash


def _plan_id_for_command(command: str) -> str:
    return "bash:" + hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]


def _allow(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle_pre_tool_use(payload: dict[str, Any], queue: ApprovalQueue) -> dict[str, Any]:
    """`payload` is the hook's stdin JSON (verified schema: `tool_name`,
    `tool_input`, `session_id`, ...). Returns the JSON to print to stdout.

    Only `Bash` is classified in this first slice -- anything else (Edit,
    Write, MCP tool calls) is denied with a clear "not yet handled" reason
    rather than silently allowed, since allowing by default on an
    unrecognized tool would be exactly the unexamined-write hole this gate
    exists to close. See `belay/hooks/decision.py` for why Bash itself
    defaults to PAUSE, not allow.
    """
    tool_name = payload.get("tool_name")
    session_id = payload.get("session_id") or "unknown-session"

    if tool_name != "Bash":
        return _deny(
            f"belay hooks: {tool_name!r} is not yet handled by the Native Agent Gate "
            "(only Bash is, so far) -- denying rather than silently allowing an "
            "unexamined tool call"
        )

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return _deny("belay hooks: Bash tool_input had no 'command' string")

    decision = classify_bash(command)
    if decision.verdict is Verdict.ALLOW:
        return _allow(f"belay hooks: {decision.reason}")

    plan_id = _plan_id_for_command(command)
    existing = queue.for_plan(plan_id)

    if existing is not None and existing.state == "approved":
        return _allow(
            f"belay hooks: this exact command was already approved "
            f"(approval {existing.approval_id}, by {existing.approved_by})"
        )
    if existing is not None and existing.state == "pending":
        return _deny(
            f"belay hooks: still pending human approval (approval {existing.approval_id}) "
            "-- run `belay approvals list` / `belay approvals approve`, then retry the "
            "same command"
        )
    if existing is not None and existing.state == "rejected":
        return _deny(
            f"belay hooks: this exact command was rejected (approval {existing.approval_id}"
            f", by {existing.rejected_by}"
            + (f": {existing.reason}" if existing.reason else "")
            + ") -- a human must run a different command, this one won't be re-asked"
        )

    # No open item (never requested, or the previous one expired -- expired
    # items are terminal too, so a fresh request is the only way forward):
    item = queue.request(
        session_id=session_id,
        plan_id=plan_id,
        # "tool" (not "tool_name"): matches the key `belay approvals list`
        # already reads via `item.plan.get('tool')` for the MCP path
        # (belay/planner/planner.py), so a hook-originated item displays
        # the same way in the same queue listing.
        plan={"tool": "Bash", "command": command, "reason": decision.reason},
    )
    return _deny(
        f"belay hooks: {decision.reason} -- queued for human approval "
        f"(approval {item.approval_id}) -- run `belay approvals list` / "
        "`belay approvals approve`, then retry the exact same command"
    )
