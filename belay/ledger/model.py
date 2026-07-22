"""Event envelope and event types (spec §9.1). Implemented in E2.

@spec("9.1")
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Complete for 0.1 (spec §9.1).
EVENT_TYPES: tuple[str, ...] = (
    "session_started",
    "contract_set_pinned",
    "plan_created",
    "policy_evaluated",
    "approval_requested",
    "approval_resolved",
    "step_journaled",
    "state_captured",
    "tool_called",
    "result_recorded",
    "compensation_registered",
    "step_committed",
    "step_failed",
    "step_indeterminate",
    "rewind_requested",
    "compensation_executed",
    "compensation_failed",
    "rewind_completed",
    "session_closed",
    "config_override",
)

# Sentinel prev_hash for the first event of a session (spec §9.1 chain root).
GENESIS_HASH: str = "0" * 64


class Event(BaseModel):
    """Ledger event envelope (spec §9.1).

    Unknown fields are preserved, never rejected — §14: "the evidence is
    tolerant, the authority is strict" (contracts/policies are strict;
    ledger events are not).
    """

    model_config = ConfigDict(extra="allow")

    event_id: str
    session_id: str
    step_seq: int | None = None
    type: str
    at: str
    payload: dict[str, Any] = Field(default_factory=dict)
    set_hash: str | None = None
    prev_hash: str
    hash: str

    def unsigned_dict(self) -> dict[str, Any]:
        """Envelope fields the hash is computed over: everything but `hash` itself."""
        return self.model_dump(mode="json", exclude={"hash"})


class VerifyReport(BaseModel):
    """Result of `verify_chain` / `verify_coherence` (spec §9.2)."""

    ok: bool
    errors: list[str] = Field(default_factory=list)
    failed_index: int | None = None
    failed_event_id: str | None = None
