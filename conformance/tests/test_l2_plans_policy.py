"""L2 -- plans, policy, approvals (spec §5, §6, §7).

@conformance(level=2)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conformance.target import ConformanceTarget
from conformance.tests.fakes import make_email_executor

pytestmark = [pytest.mark.anyio, pytest.mark.l2]

REPO_ROOT = Path(__file__).resolve().parents[2]
EMAIL_CONTRACT = REPO_ROOT / "examples" / "contracts" / "email.yaml"


async def test_irreversible_call_pauses_by_default(target: ConformanceTarget) -> None:
    """spec §6.4: `defaults.irreversible: pause` out of the box -- no policy file needed."""
    session_id = target.new_session([EMAIL_CONTRACT], make_email_executor())
    result = await target.call(
        session_id, "email.send", {"to": ["a@example.com"], "body": "hi"}
    )
    assert result["status"] == "pending_approval"

    events = [e.type for e in target.ledger(session_id)]
    assert "plan_created" in events
    assert "policy_evaluated" in events
    assert "approval_requested" in events


async def test_approved_plan_proceeds_to_execution(target: ConformanceTarget) -> None:
    """spec §7: only an explicit operator `approve` (never the agent path) unparks a plan."""
    session_id = target.new_session([EMAIL_CONTRACT], make_email_executor())
    args = {"to": ["a@example.com"], "body": "hi"}
    pending = await target.call(session_id, "email.send", args)
    assert pending["status"] == "pending_approval"

    target.approve(session_id, pending["approval_id"], approved_by="conformance-operator")
    result = await target.call(session_id, "email.send", args)
    assert "status" not in result  # a real tool result, not another pending_approval envelope

    events = [e.type for e in target.ledger(session_id)]
    assert "step_committed" in events


async def test_agent_facing_call_path_has_no_self_approval(target: ConformanceTarget) -> None:
    """spec §7.2: the same paused plan, called again unapproved, stays parked."""
    session_id = target.new_session([EMAIL_CONTRACT], make_email_executor())
    args = {"to": ["a@example.com"], "body": "hi"}
    first = await target.call(session_id, "email.send", args)
    second = await target.call(session_id, "email.send", args)
    assert first["status"] == second["status"] == "pending_approval"
    assert first["approval_id"] == second["approval_id"]
