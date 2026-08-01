"""`ActionEnvelope` (R1.7.3, ADR 0025): the shared shape both of Belay's
two decision engines' per-call inputs already normalize into, made
explicit for the first time.

Purely additive, by design (see ADR 0025's "R1.7.3 (revised)" section for
why the originally-sketched next step -- making the hooks path call the
MCP proxy's `Planner.plan()`/`PolicyEngine.evaluate()` directly -- turned
out to be the wrong direction once actually investigated). Neither
conversion function below is called from any production decision path:
this module only proves the two engines' "what identifies this specific
call" concepts are already the same shape, it does not change what
either engine does with that shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from belay.supervisor.protocol import HookEvent


@dataclass(frozen=True)
class ActionEnvelope:
    """One tool call, in the shape both engines' inputs already carry.

    `repo_prestate_digest`/`os_identity` are honestly `None` for the MCP
    proxy side (`from_mcp_call`) -- it has no equivalent concept today,
    not invented here to force a fit. `event_id` is the field that binds
    a `CapabilityLease` (`ApprovalQueue.consume`) to a specific call
    instance on both engines -- see `from_mcp_call`'s docstring for why
    its value exactly matches what `belay/proxy/lifecycle.py::ApprovalStage`
    already builds.
    """

    surface: str
    host: str
    tool: str
    args: dict[str, Any]
    session_id: str
    cwd: str | None
    repo_prestate_digest: str | None
    os_identity: str | None
    event_id: str
    monotonic_ns: int | None = None
    wall_clock: str | None = None


def from_hook_event(event: HookEvent) -> ActionEnvelope:
    """`belay/supervisor/protocol.py::HookEvent` -> `ActionEnvelope`, a
    1:1 field mapping -- `HookEvent` already carries everything this
    shape needs, nothing is inferred or defaulted."""
    return ActionEnvelope(
        surface=event.surface,
        host=event.host,
        tool=event.tool_name,
        args=event.args,
        session_id=event.host_session_id,
        cwd=event.cwd,
        repo_prestate_digest=event.repo_identity,
        os_identity=event.os_user,
        event_id=event.event_id,
        monotonic_ns=event.monotonic_ns,
        wall_clock=event.wall_clock,
    )


def from_mcp_call(
    *, session_id: str, step_seq: int, tool: str, args: dict[str, Any], cwd: str | None = None
) -> ActionEnvelope:
    """The MCP proxy's per-call inputs -> `ActionEnvelope`.

    `event_id=f"{session_id}:{step_seq}"` is not a new convention invented
    for this module -- it is the *exact* identity
    `belay/proxy/lifecycle.py::ApprovalStage.check()` already builds to
    key its `ApprovalQueue.consume()` `CapabilityLease` claim (R1.7.1).
    Reusing it here, rather than picking a different shape, is the point:
    it proves the two engines' "what identifies this specific call"
    concepts already agree, they just never had one named type to say so.

    `repo_prestate_digest`/`os_identity` are `None` -- the MCP proxy has
    no working-tree-prestate or OS-identity concept today (its identity
    is `session_started.initiated_by`, a human-supplied attribution
    string, a different concept entirely). Left honestly `None` rather
    than papered over with a value that doesn't mean the same thing.
    """
    return ActionEnvelope(
        surface="mcp",
        host="mcp",
        tool=tool,
        args=args,
        session_id=session_id,
        cwd=cwd,
        repo_prestate_digest=None,
        os_identity=None,
        event_id=f"{session_id}:{step_seq}",
    )
