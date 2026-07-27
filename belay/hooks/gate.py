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
from typing import Literal

from belay.approvals.queue import ApprovalQueue
from belay.hooks.decision import DECISION_LOGIC_VERSION, Verdict, classify_bash
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


def _plan_id_for_event(event: HookEvent, command: str) -> str:
    """Binds an approval to the full context it was granted in, not just the
    command text: host, session, tool, the command itself, cwd, the
    repository's real git HEAD (see `claude_code_adapter._repo_identity`),
    and the decision-logic ruleset version (so a rule change never silently
    reinterprets an approval granted under the old rules)."""
    material = json.dumps(
        {
            "decision_logic_version": DECISION_LOGIC_VERSION,
            "host": event.host,
            "host_session_id": event.host_session_id,
            "tool_name": event.tool_name,
            "command": command,
            "cwd": event.cwd,
            "repo_identity": event.repo_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "bash:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


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
