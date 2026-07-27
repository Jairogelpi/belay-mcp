"""`belay causal`: the causal graph of one session, from the ledger alone.

Not a new subsystem -- every edge here already exists as a ledger event
(spec §9): `state_captured` ("what did it read before deciding"),
`plan_created`'s `intent_id`/`test_ref` (adoption/DX tags, see
`belay/proxy/server.py`), `policy_evaluated` ("what decision fired"),
`step_committed`/`compensation_registered` ("what happened, how to undo
it"), `belay:test_verified` (`belay/ledger/test_evidence.py` -- a test
actually *run*, not just named). This module only assembles what's
already there into one graph and renders it, instead of leaving a human
to grep the ledger by hand.

Test proof is three-tiered, not binary: `test_verified=True` means a
`belay:test_verified` event with `exit_code == 0` exists for that step
(`belay verify-test` actually ran it); `test_ref` alone (no matching
`belay:test_verified`) is merely *claimed* -- an agent-supplied label,
never independently checked.

`depends_on` is the one inferred edge (not directly a ledger field): a
step depends on the nearest earlier step that touched the same `path` --
a plain same-resource heuristic, not a real data/control-flow analysis;
documented as such rather than oversold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from belay.ledger.model import Event


@dataclass
class CausalNode:
    step_seq: int
    tool: str
    args: dict[str, Any]
    intent_id: str | None
    test_ref: str | None
    read_before: dict[str, Any] | None
    policy_verdict: str | None
    policy_reasons: list[str]
    status: str | None  # step_committed / step_failed / ...
    compensation_tool: str | None
    test_verified: bool | None = None  # True/False: belay:test_verified ran; None: never run
    test_evidence: dict[str, Any] | None = None
    depends_on: list[int] = field(default_factory=list)


def build_causal_graph(events: list[Event]) -> list[CausalNode]:
    plans: dict[int, dict[str, Any]] = {}
    captures: dict[int, dict[str, Any]] = {}
    policies: dict[int, dict[str, Any]] = {}
    statuses: dict[int, str] = {}
    compensations: dict[int, str] = {}
    test_evidence: dict[int, dict[str, Any]] = {}

    for event in events:
        if event.step_seq is None:
            continue
        s = event.step_seq
        if event.type == "plan_created":
            plans[s] = event.payload
        elif event.type == "state_captured":
            captures[s] = event.payload.get("snapshot") or {}
        elif event.type == "policy_evaluated":
            policies[s] = event.payload
        elif event.type in ("step_committed", "step_failed", "step_indeterminate"):
            statuses[s] = event.type
        elif event.type == "compensation_registered":
            compensations[s] = event.payload.get("tool", "")
        elif event.type == "belay:test_verified":
            test_evidence[s] = event.payload  # latest wins if re-run

    nodes: list[CausalNode] = []
    path_last_step: dict[str, int] = {}
    for step_seq in sorted(plans):
        plan = plans[step_seq]
        args = plan.get("args") or {}
        policy = policies.get(step_seq, {})
        evidence = test_evidence.get(step_seq)
        declared_test_ref = plan.get("test_ref")
        # Only mode="test" (belay verify-test --runner, command built mechanically
        # from the step's OWN test_ref) counts as verified -- mode="command" (a
        # free-form --cmd) is a real command that ran, but proves nothing about
        # any specific declared test, so it must never show as VERIFIED here.
        # The test_ref match is a second guard: evidence recorded against a
        # different ref than this step currently declares is stale, not proof.
        is_real_test_verification = bool(
            evidence
            and evidence.get("mode") == "test"
            and evidence.get("test_ref") == declared_test_ref
        )
        node = CausalNode(
            step_seq=step_seq,
            tool=plan.get("tool", ""),
            args=args,
            intent_id=plan.get("intent_id"),
            test_ref=declared_test_ref,
            read_before=captures.get(step_seq),
            policy_verdict=policy.get("verdict"),
            policy_reasons=policy.get("reasons", []),
            status=statuses.get(step_seq),
            compensation_tool=compensations.get(step_seq),
            test_verified=(evidence or {}).get("passed") if is_real_test_verification else None,
            test_evidence=evidence if is_real_test_verification else None,
        )
        path = args.get("path")
        if isinstance(path, str) and path in path_last_step:
            node.depends_on.append(path_last_step[path])
        if isinstance(path, str):
            path_last_step[path] = step_seq
        nodes.append(node)
    return nodes


def to_mermaid(nodes: list[CausalNode], session_id: str) -> str:
    lines = ["flowchart TD", f'  R["request: {session_id}"]']
    seen_intents: set[str] = set()
    for node in nodes:
        step_label = f"S{node.step_seq}"
        path = node.args.get("path")
        tool_label = f"{node.tool}({path})" if path else node.tool
        if node.status:
            tool_label += f" [{node.status}]"
        lines.append(f'  {step_label}["{tool_label}"]')
        if node.intent_id:
            intent_node = f"I_{node.intent_id.replace(' ', '_').replace('-', '_')}"
            if node.intent_id not in seen_intents:
                lines.append(f'  {intent_node}["intent: {node.intent_id}"]')
                lines.append(f"  R --> {intent_node}")
                seen_intents.add(node.intent_id)
            lines.append(f"  {intent_node} --> {step_label}")
        else:
            lines.append(f"  R --> {step_label}")
        if node.test_ref:
            test_node = f"T{node.step_seq}"
            if node.test_verified is True:
                lines.append(f'  {test_node}(["VERIFIED: {node.test_ref}"])')
                lines.append(f"  {test_node} ==proves==> {step_label}")
            elif node.test_verified is False:
                lines.append(f'  {test_node}(["FAILED: {node.test_ref}"])')
                lines.append(f"  {test_node} -.disproves.-> {step_label}")
            else:
                lines.append(f'  {test_node}(["claimed, never run: {node.test_ref}"])')
                lines.append(f"  {test_node} -.claims (unverified).-> {step_label}")
        for dep in node.depends_on:
            lines.append(f"  S{dep} --> {step_label}")
    return "\n".join(lines)
