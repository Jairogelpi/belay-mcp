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
concept needed. `plan_id` is a deterministic hash of the exact command
string, so a second attempt at the identical command finds the same
approval item instead of opening a new one -- and once a human approves it,
every future identical command in that session is allowed without asking
again (deliberate: the human approved *this exact string*, not "trust this
agent from now on").
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from belay.approvals.queue import ApprovalQueue
from belay.hooks.decision import Verdict, classify_bash
from belay.supervisor.protocol import HookEvent


@dataclass(frozen=True)
class GateDecision:
    verdict: Literal["allow", "deny"]
    reason: str
    approval_id: str | None = None


def _plan_id_for_command(command: str) -> str:
    return "bash:" + hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]


def evaluate(event: HookEvent, queue: ApprovalQueue) -> GateDecision:
    """Only pre-phase Bash calls are classified in this first slice --
    everything else (post-phase, Edit/Write, MCP tool calls) is denied with
    a clear "not yet handled" reason rather than silently allowed, since
    allowing by default on an unrecognized surface would be exactly the
    unexamined-write hole this gate exists to close. See
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
            "Native Agent Gate (only Bash is, so far) -- denying rather than silently "
            "allowing an unexamined tool call",
        )

    command = event.args.get("command")
    if not isinstance(command, str):
        return GateDecision("deny", "belay: Bash call had no 'command' string")

    decision = classify_bash(command)
    if decision.verdict is Verdict.ALLOW:
        return GateDecision("allow", f"belay: {decision.reason}")

    plan_id = _plan_id_for_command(command)
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
